"""Bundled iroh transport: an in-process QUIC endpoint the hub accepts on.

iroh embeds in the hub process. It needs no account, no daemon and no port
forwarding: it hole-punches a direct connection to the client and falls back to
n0's public relays when hole-punching fails. The user configures nothing.

The endpoint identity is a secret key stored beside the other AgnView state in
~/.agnview, so the ticket printed into the pairing QR code keeps working across
restarts. Nothing about it is ever shown to the user.

Reachability is never on the startup path. start() schedules the work and
returns, so a hub with no route to the internet still starts and still serves
the dashboard over LAN.

A connection carries one or more bidirectional streams, each with one request.
A request with no "op" key streams the console, as it always has. A request
with "op": "api" is one call to the mobile API, answered by iroh_api.
"""

import asyncio
import json
import logging
import os
import secrets
import socket
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional

from . import iroh_api
from .network import Transport, resolve_iroh_path_transport
from .pairing import check_auth_rate_limit, record_failed_auth, reset_auth_rate_limit

logger = logging.getLogger("agnview.iroh")

# Application-layer protocol name negotiated on every iroh connection.
IROH_ALPN = b"agnview/console/1"

# Wire protocol version spoken over IROH_ALPN.
IROH_PROTOCOL_VERSION = 1

SECRET_KEY_FILE = Path.home() / ".agnview" / "iroh_secret"

# Binding the endpoint is local work, but it talks to the relay discovery code
# on the way. Cap it so a black-holed network cannot pin the task open forever.
IROH_BIND_TIMEOUT_SECONDS = 30.0

# How often a streaming connection polls the console buffer for new rows.
CONSOLE_POLL_SECONDS = 0.5

# How often a streaming connection sends a keep-alive frame when idle.
CONSOLE_PING_SECONDS = 15.0

MAX_REQUEST_BYTES = 64 * 1024

# How long a client has to send its request and finish its send side.
REQUEST_READ_TIMEOUT_SECONDS = 30.0

# Streams a client may hold open on one connection at once: the console plus
# a few API calls. QUIC holds any further stream back until one ends.
MAX_STREAMS_PER_CONNECTION = 8

# How long an error frame is given to reach the client before the connection
# that carried it is closed.
FLUSH_BEFORE_CLOSE_SECONDS = 2.0

# The client address the in-process API request carries. The hub's own auth
# middleware counts a wrong key from it under this name.
IROH_RATE_LIMIT_KEY = iroh_api.IROH_CLIENT_ADDRESS[0]

# Wrong pairing keys are counted per peer, in the limiter the LAN uses, under
# "iroh:" plus the peer's endpoint id. A valid key is checked first and is never
# refused. A peer id costs nothing to make, so a second cap counts wrong keys
# across all peers. It throttles only wrong keys, so it cannot lock out a
# valid one.
IROH_PEER_KEY_PREFIX = IROH_RATE_LIMIT_KEY + ":"
IROH_GLOBAL_INVALID_LIMIT = 60
IROH_GLOBAL_WINDOW_SECONDS = 60.0


class _RequestTooLarge(Exception):
    pass


def _set_posix_permissions_0600(file_path: Path) -> None:
    try:
        os.chmod(file_path, 0o600)
    except Exception:
        pass


def get_or_create_iroh_secret(path: Optional[Path] = None) -> bytes:
    """Return the persistent 32-byte endpoint secret, creating it on first run.

    This is the only key iroh needs and the user never sees it. It is created
    silently, exactly like the pairing token beside it.
    """
    key_file = path or SECRET_KEY_FILE
    if key_file.exists():
        try:
            stored = key_file.read_text(encoding="utf-8").strip()
            raw = bytes.fromhex(stored)
            if len(raw) == 32:
                return raw
        except Exception:
            logger.warning("iroh secret at %s is unreadable, generating a new one", key_file)

    key_file.parent.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_bytes(32)
    key_file.write_text(raw.hex(), encoding="utf-8")
    _set_posix_permissions_0600(key_file)
    return raw


def iroh_available() -> bool:
    """Return True when the iroh wheel imports on this platform."""
    try:
        import iroh  # noqa: F401
    except Exception:
        return False
    return True


class IrohTransport(Transport):
    """Accept AgnView console and mobile API connections over iroh.

    States reported by status()['state']:
      disabled     turned off by configuration
      unavailable  no iroh wheel for this platform
      starting     bind scheduled, not yet complete
      ready        accepting connections, ticket available
      failed       start failed, the reason is in status()['error']
    """

    name = "iroh"

    def __init__(
        self,
        db: Any = None,
        token_provider: Optional[Callable[[], Optional[str]]] = None,
        relay_url: str = "",
        enabled: bool = True,
        disabled_reason: str = "",
        secret_key_path: Optional[Path] = None,
        asgi_app: Any = None,
        api_enabled: bool = True,
    ):
        self._db = db
        self._token_provider = token_provider
        self._relay_url = (relay_url or "").strip()
        self._enabled = enabled
        self._secret_key_path = secret_key_path
        # The hub's own FastAPI app. API mode hands requests to it in-process.
        self._asgi_app = asgi_app
        self._api_enabled = api_enabled
        self._api_slots = iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_TOTAL)
        # When each recent invalid key arrived, across every peer.
        self._invalid_attempts: Deque[float] = deque()

        self._state = "disabled" if not enabled else "starting"
        self._error = disabled_reason if not enabled else ""
        self._endpoint = None
        self._ticket: Optional[str] = None
        self._node_id: Optional[str] = None
        self._home_relay: Optional[str] = None
        self._direct_addresses: List[str] = []
        self._last_resolved: Optional[str] = None
        self._connections = 0

        self._start_task: Optional[asyncio.Task] = None
        self._accept_task: Optional[asyncio.Task] = None
        self._handlers: List[asyncio.Task] = []
        self._closing = False

    # ----------------- Transport interface -----------------

    def start(self) -> None:
        """Schedule the endpoint bind. Returns immediately, always."""
        if not self._enabled:
            logger.info("iroh transport disabled: %s", self._error or "turned off by configuration")
            return
        if not iroh_available():
            self._state = "unavailable"
            self._error = (
                "the iroh package does not provide a wheel for this platform, "
                "so remote access stays LAN only"
            )
            logger.warning("iroh transport unavailable: %s", self._error)
            return
        if self._start_task is not None:
            return

        self._state = "starting"
        self._start_task = asyncio.ensure_future(self._start_background())

    async def stop(self) -> None:
        """Close the endpoint and every connection it is serving."""
        self._closing = True
        for task in (self._start_task, self._accept_task):
            if task is not None and not task.done():
                task.cancel()
        for task in list(self._handlers):
            if not task.done():
                task.cancel()
        self._handlers.clear()

        endpoint = self._endpoint
        self._endpoint = None
        if endpoint is not None:
            try:
                await endpoint.close()
            except Exception as exc:
                logger.debug("closing the iroh endpoint raised %s", exc)
        if self._state == "ready":
            self._state = "disabled"
            self._error = "stopped"

    def status(self) -> Dict[str, Any]:
        """Describe the transport for the API and the pairing payload."""
        return {
            "name": self.name,
            "state": self._state,
            "error": self._error or None,
            "ticket": self._ticket,
            "node_id": self._node_id,
            "relay_url": self._home_relay,
            "configured_relay_url": self._relay_url or None,
            "direct_addresses": list(self._direct_addresses),
            "connections": self._connections,
            "last_resolved_transport": self._last_resolved,
            "capabilities": self.capabilities,
        }

    # ----------------- Convenience -----------------

    @property
    def ticket(self) -> Optional[str]:
        """Return the node ticket once the endpoint is up, otherwise None."""
        return self._ticket

    @property
    def state(self) -> str:
        return self._state

    @property
    def last_resolved_transport(self) -> Optional[str]:
        return self._last_resolved

    @property
    def api_enabled(self) -> bool:
        """True when API mode is switched on, whether or not an app is attached."""
        return self._api_enabled

    @property
    def capabilities(self) -> List[str]:
        """What a client can ask for on this hub, as sent in the hello frame."""
        if self._api_enabled and self._asgi_app is not None:
            return ["console", "api"]
        return ["console"]

    # ----------------- Endpoint lifecycle -----------------

    async def _start_background(self) -> None:
        try:
            await asyncio.wait_for(self._bind(), timeout=IROH_BIND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._state = "failed"
            self._error = f"binding the iroh endpoint timed out after {IROH_BIND_TIMEOUT_SECONDS:.0f}s"
            logger.warning("iroh transport did not start: %s", self._error)
        except Exception as exc:
            self._state = "failed"
            self._error = f"binding the iroh endpoint failed: {exc}"
            logger.warning("iroh transport did not start: %s", self._error)

    async def _bind(self) -> None:
        import iroh

        secret = get_or_create_iroh_secret(self._secret_key_path)
        options: Dict[str, Any] = {
            "preset": iroh.preset_n0(),
            "secret_key": secret,
            "alpns": [IROH_ALPN],
        }
        if self._relay_url:
            options["relay_mode"] = iroh.RelayMode.custom_from_urls([self._relay_url])

        endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(**options))
        if self._closing:
            await endpoint.close()
            return

        self._endpoint = endpoint
        addr = endpoint.addr()
        self._node_id = addr.id().fmt_short()
        self._home_relay = addr.relay_url() or (self._relay_url or None)
        self._direct_addresses = list(addr.direct_addresses() or [])
        self._ticket = str(iroh.EndpointTicket.from_addr(addr))
        self._state = "ready"
        self._error = ""
        logger.info("iroh transport ready, node %s", self._node_id)

        self._accept_task = asyncio.ensure_future(self._accept_loop())

    async def _accept_loop(self) -> None:
        endpoint = self._endpoint
        while endpoint is not None and not self._closing:
            try:
                incoming = await endpoint.accept_next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._closing:
                    logger.debug("iroh accept loop stopped: %s", exc)
                return
            if incoming is None:
                return
            task = asyncio.ensure_future(self._handle_incoming(incoming))
            self._handlers.append(task)
            self._handlers = [t for t in self._handlers if not t.done()]

    # ----------------- Connection handling -----------------

    async def _handle_incoming(self, incoming: Any) -> None:
        conn = None
        streams: List[asyncio.Task] = []
        try:
            accepting = await incoming.accept()
            conn = await accepting.connect()
            self._connections += 1
            resolved = resolve_iroh_path_transport(conn.paths())
            self._last_resolved = resolved
            try:
                conn.set_max_concurrent_bi_streams(MAX_STREAMS_PER_CONNECTION)
            except Exception as exc:
                logger.debug("could not cap streams on an iroh connection: %s", exc)

            # Every stream is one request. A console request holds its stream
            # and closes the connection when it ends, as it always has. An API
            # request ends its stream and leaves the connection open for the
            # next one, so a phone need not reconnect for every call.
            slots = iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_PER_CONNECTION)
            while not self._closing:
                try:
                    bi = await conn.accept_bi()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("iroh connection ended: %s", exc)
                    break
                streams = [t for t in streams if not t.done()]
                streams.append(asyncio.ensure_future(self._handle_stream(conn, bi, slots)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh connection ended: %s", exc)
        finally:
            for task in streams:
                if not task.done():
                    task.cancel()
            if conn is not None:
                self._close_connection(conn)

    async def _handle_stream(self, conn: Any, bi: Any, slots: "iroh_api.ApiSlots") -> None:
        """Serve the one request a stream carries."""
        recv = bi.recv()
        send = bi.send()
        keep_connection = False
        try:
            try:
                raw = await asyncio.wait_for(
                    self._read_request(recv), timeout=REQUEST_READ_TIMEOUT_SECONDS
                )
            except _RequestTooLarge:
                await self._write_error(send, iroh_api.ERROR_TOO_LARGE)
                return
            except asyncio.TimeoutError:
                await self._write_error(send, iroh_api.ERROR_TIMEOUT)
                return

            request = self._parse_request(raw)
            if request is None:
                await self._write_error(send, "malformed request")
                return

            op = request.get("op")
            # The key is checked first, in constant time. A valid key always
            # proceeds, so no amount of wrong guesses from anyone can lock the
            # owner out. Only invalid attempts are counted and throttled: per
            # peer, and across all peers with a separate cap. API mode can
            # dispatch prompts, so it always needs a pairing key, even on a
            # hub that runs without one.
            peer_key = self._peer_limit_key(conn)
            if not self._authorised(request.get("token"), require_token=op is not None):
                if not check_auth_rate_limit(peer_key) or not self._global_invalid_allowed():
                    await self._write_error(send, iroh_api.ERROR_RATE_LIMITED)
                    return
                record_failed_auth(peer_key)
                self._record_global_invalid()
                await self._write_error(send, iroh_api.ERROR_UNAUTHORISED)
                return
            reset_auth_rate_limit(peer_key)

            if op is None:
                await self._stream_console(conn, send, request)
                return

            keep_connection = True
            await self._serve_api(send, request, slots)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh stream ended: %s", exc)
        finally:
            await self._finish(send, wait=not keep_connection)
            if not keep_connection:
                self._close_connection(conn)

    async def _serve_api(self, send: Any, request: Dict[str, Any], slots: "iroh_api.ApiSlots") -> None:
        """Answer one API request: hello, then one response or error frame."""
        await self._write_frame(send, self._hello_frame())

        started = time.monotonic()
        method, template, outcome = "-", "-", ""
        try:
            if request.get("op") != iroh_api.API_OP:
                raise iroh_api.ApiError(iroh_api.ERROR_BAD_REQUEST)
            if "api" not in self.capabilities:
                raise iroh_api.ApiError(iroh_api.ERROR_FORBIDDEN_PATH)
            api_request = iroh_api.parse_api_request(request)
            method, template = api_request.method, api_request.template
            with iroh_api.claim(slots, self._api_slots):
                frame = await iroh_api.forward_to_app(
                    self._asgi_app, api_request, token=self._expected_token()
                )
            outcome = str(frame["status"])
        except iroh_api.ApiError as exc:
            frame = {"type": "error", "detail": exc.code}
            outcome = exc.code

        # Method, route template, outcome and time only. Never the key, the
        # body or the query.
        logger.info(
            "iroh api %s %s -> %s in %.0f ms",
            method, template, outcome, (time.monotonic() - started) * 1000,
        )
        await self._write_frame(send, frame)

    @staticmethod
    async def _read_request(recv: Any) -> bytes:
        """Read the request up to the client's end of stream, within the size cap."""
        data = bytearray()
        while True:
            chunk = await recv.read(MAX_REQUEST_BYTES + 1 - len(data))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
            if len(data) > MAX_REQUEST_BYTES:
                try:
                    await recv.stop(0)
                except Exception:
                    pass
                raise _RequestTooLarge()

    async def _write_error(self, send: Any, detail: str) -> None:
        await self._write_frame(send, {"type": "error", "detail": detail})

    @staticmethod
    async def _finish(send: Any, wait: bool) -> None:
        """End our side of the stream, and give the last frame time to land
        when the connection is about to close under it."""
        try:
            await send.finish()
            if wait:
                await asyncio.wait_for(send.stopped(), timeout=FLUSH_BEFORE_CLOSE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    @staticmethod
    def _close_connection(conn: Any) -> None:
        try:
            conn.close(0, b"bye")
        except Exception:
            pass

    @staticmethod
    def _parse_request(raw: bytes) -> Optional[Dict[str, Any]]:
        try:
            text = raw.decode("utf-8").strip()
            if not text:
                return {}
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except Exception:
            return None

    @staticmethod
    def _peer_limit_key(conn: Any) -> str:
        """The limiter key for the peer on this connection."""
        try:
            text = str(conn.remote_id()).strip()
        except Exception:
            text = ""
        return f"{IROH_PEER_KEY_PREFIX}{text or 'unknown'}"

    def _global_invalid_allowed(self) -> bool:
        now = time.monotonic()
        recent = self._invalid_attempts
        while recent and now - recent[0] >= IROH_GLOBAL_WINDOW_SECONDS:
            recent.popleft()
        return len(recent) < IROH_GLOBAL_INVALID_LIMIT

    def _record_global_invalid(self) -> None:
        self._invalid_attempts.append(time.monotonic())

    def _expected_token(self) -> Optional[str]:
        if self._token_provider is None:
            return None
        try:
            return self._token_provider()
        except Exception:
            return None

    def _authorised(self, provided: Optional[str], require_token: bool = False) -> bool:
        expected = self._expected_token()
        if not expected:
            return not require_token
        return bool(provided) and secrets.compare_digest(str(provided), str(expected))

    def _hello_frame(self) -> Dict[str, Any]:
        return {
            "type": "hello",
            "app": "AgnView",
            "protocol": IROH_PROTOCOL_VERSION,
            "hostname": socket.gethostname(),
            "transport": self._last_resolved,
            "capabilities": self.capabilities,
        }

    async def _stream_console(self, conn: Any, send: Any, request: Dict[str, Any]) -> None:
        agent = request.get("agent") or "all"
        backlog = min(max(int(request.get("backlog") or 200), 0), iroh_api.MAX_PAGE_SIZE)
        after_id = request.get("after_id")
        after_id = int(after_id) if after_id is not None else None

        await self._write_frame(send, self._hello_frame())

        rows = await self._read_console(agent=agent, limit=backlog, after_id=after_id)
        for row in rows:
            after_id = max(after_id or 0, int(row.get("id") or 0))
            await self._write_frame(send, self._log_frame(row))

        idle = 0.0
        while not self._closing:
            await asyncio.sleep(CONSOLE_POLL_SECONDS)
            rows = await self._read_console(agent=agent, limit=500, after_id=after_id)
            if rows:
                idle = 0.0
                for row in rows:
                    after_id = max(after_id or 0, int(row.get("id") or 0))
                    await self._write_frame(send, self._log_frame(row))
                continue

            idle += CONSOLE_POLL_SECONDS
            if idle >= CONSOLE_PING_SECONDS:
                idle = 0.0
                self._last_resolved = resolve_iroh_path_transport(conn.paths())
                await self._write_frame(send, {"type": "ping", "transport": self._last_resolved})

    async def _read_console(self, agent: str, limit: int, after_id: Optional[int]) -> List[Dict[str, Any]]:
        if self._db is None:
            return []
        loop = asyncio.get_running_loop()

        def _query() -> List[Dict[str, Any]]:
            return self._db.get_console_logs(agent=agent, limit=limit, after_id=after_id)

        try:
            rows = await loop.run_in_executor(None, _query)
        except Exception as exc:
            logger.debug("console read failed: %s", exc)
            return []
        return sorted(rows or [], key=lambda r: int(r.get("id") or 0))

    @staticmethod
    def _log_frame(row: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "type": "log",
            "id": row.get("id"),
            "agent": row.get("agent"),
            "source": row.get("source"),
            "content": row.get("content"),
            "timestamp": row.get("timestamp"),
            "session_id": row.get("session_id"),
        }

    @staticmethod
    async def _write_frame(send: Any, frame: Dict[str, Any]) -> None:
        await send.write_all((json.dumps(frame) + "\n").encode("utf-8"))
