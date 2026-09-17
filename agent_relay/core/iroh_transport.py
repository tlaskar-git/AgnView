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
"""

import asyncio
import json
import logging
import os
import secrets
import socket
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .network import Transport, resolve_iroh_path_transport

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
    """Accept AgnView console connections over iroh.

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
    ):
        self._db = db
        self._token_provider = token_provider
        self._relay_url = (relay_url or "").strip()
        self._enabled = enabled
        self._secret_key_path = secret_key_path

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
        try:
            accepting = await incoming.accept()
            conn = await accepting.connect()
            self._connections += 1
            resolved = resolve_iroh_path_transport(conn.paths())
            self._last_resolved = resolved

            bi = await conn.accept_bi()
            recv = bi.recv()
            send = bi.send()

            raw = await recv.read_to_end(MAX_REQUEST_BYTES)
            request = self._parse_request(raw)
            if request is None:
                await self._write_frame(send, {"type": "error", "detail": "malformed request"})
                return

            if not self._authorised(request.get("token")):
                await self._write_frame(send, {"type": "error", "detail": "unauthorised"})
                return

            await self._stream_console(conn, send, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("iroh connection ended: %s", exc)
        finally:
            if conn is not None:
                try:
                    await conn.close(0, b"bye")
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

    def _authorised(self, provided: Optional[str]) -> bool:
        expected = None
        if self._token_provider is not None:
            try:
                expected = self._token_provider()
            except Exception:
                expected = None
        if not expected:
            return True
        return bool(provided) and secrets.compare_digest(str(provided), str(expected))

    async def _stream_console(self, conn: Any, send: Any, request: Dict[str, Any]) -> None:
        agent = request.get("agent") or "all"
        backlog = int(request.get("backlog") or 200)
        after_id = request.get("after_id")
        after_id = int(after_id) if after_id is not None else None

        await self._write_frame(send, {
            "type": "hello",
            "app": "AgnView",
            "protocol": IROH_PROTOCOL_VERSION,
            "hostname": socket.gethostname(),
            "transport": self._last_resolved,
        })

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
