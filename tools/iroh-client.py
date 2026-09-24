#!/usr/bin/env python3
"""Stream an AgnView hub's console from anywhere, over LAN or over iroh.

This is the reference client for the transport. It exists so the iroh path can
be exercised end to end without a phone, and so the connection order in
docs/PAIRING.md has a working implementation to check against.

Give it whatever the hub handed you:

  # the pairing payload from GET /api/mobile/pairing
  python tools/iroh-client.py --payload "$(curl -s localhost:8765/api/mobile/pairing | jq -c .pairing)"

  # the deep link out of the QR code
  python tools/iroh-client.py "agnview://pair?v=2&name=box&lan=10.0.0.2:8765&fp=&id=x&k=secret&iroh=endpointa..."

  # a bare ticket, with the token supplied separately
  python tools/iroh-client.py endpointa... --token <pairing token>

Connection order, as defined in docs/PAIRING.md:

  1. the LAN address, given 800ms to answer
  2. iroh, hole-punched direct
  3. iroh, through a relay

The resolved rung is printed to stderr before the first line of output, so
stdout carries nothing but console lines.
"""

import argparse
import asyncio
import json
import socket
import sys
import time
import urllib.parse
from typing import Any, Dict, Optional

# Keep these in step with agent_relay/core/network.py and
# agent_relay/core/iroh_transport.py. This file runs standalone, so it does not
# import the hub package.
IROH_ALPN = b"agnview/console/1"
LAN_CONNECT_TIMEOUT_SECONDS = 0.8
LAN_POLL_SECONDS = 0.5


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ----------------- Reading whatever the user pasted -----------------

def parse_source(raw: str) -> Dict[str, Any]:
    """Turn a JSON payload, a pairing URI or a bare ticket into one dict."""
    text = (raw or "").strip()
    if not text:
        raise SystemExit("nothing to connect to")

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"the payload is not valid JSON: {exc}")
        if not isinstance(payload, dict):
            raise SystemExit("the payload must be a JSON object")
        return _from_payload(payload)

    if text.startswith("agnview://pair?"):
        return _from_uri(text)

    return {"ticket": text, "token": None, "lan": None, "name": None}


def _from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    deeplink = payload.get("deeplink") or payload.get("uri") or payload.get("deep_link")
    parsed = _from_uri(deeplink) if deeplink else {"ticket": None, "token": None, "lan": None, "name": None}

    ticket = payload.get("iroh_ticket") or parsed.get("ticket")
    token = payload.get("token") or payload.get("pairing_token") or parsed.get("token")

    lan = parsed.get("lan")
    endpoints = payload.get("endpoints") or {}
    if not lan:
        primary = payload.get("primary_url") or endpoints.get("lan") or endpoints.get("localhost")
        if primary:
            split = urllib.parse.urlsplit(primary)
            if split.netloc:
                lan = split.netloc

    return {
        "ticket": ticket,
        "token": token,
        "lan": lan,
        "name": payload.get("hostname") or parsed.get("name"),
    }


def _from_uri(uri: str) -> Dict[str, Any]:
    if "?" not in uri:
        raise SystemExit("the pairing URI carries no fields")
    params = urllib.parse.parse_qs(uri.split("?", 1)[1], keep_blank_values=True)

    def one(key: str) -> Optional[str]:
        values = params.get(key) or [""]
        return values[0] or None

    version = one("v")
    if version not in ("1", "2"):
        raise SystemExit(f"unsupported pairing payload version {version!r}")

    return {
        "ticket": one("iroh"),
        "token": one("k"),
        "lan": one("lan"),
        "name": urllib.parse.unquote(one("name") or "") or None,
    }


# ----------------- Rung 1: the LAN address -----------------

def lan_reachable(lan: Optional[str], timeout: float = LAN_CONNECT_TIMEOUT_SECONDS) -> bool:
    """Return True when the LAN address answers inside the budget."""
    if not lan:
        return False
    host, _, port = lan.rpartition(":")
    if not host or not port.isdigit():
        return False
    if host in ("127.0.0.1", "localhost", "::1", "[::1]"):
        # The hub listens on its own machine only, so there is no LAN rung.
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def stream_over_lan(lan: str, token: Optional[str], agent: str, backlog: int) -> int:
    """Follow the console over the hub's HTTP API."""
    try:
        import httpx
    except ImportError:
        log("httpx is not installed, so the LAN rung is unavailable")
        return 2

    base = f"http://{lan}"
    headers = {"X-AgnView-Token": token} if token else {}
    after_id: Optional[int] = None
    limit = backlog

    log(f"resolved transport: lan ({base})")
    with httpx.Client(timeout=10.0, headers=headers) as client:
        while True:
            params: Dict[str, Any] = {"agent": agent, "limit": limit}
            if after_id is not None:
                params["after_id"] = after_id
            response = client.get(f"{base}/api/console/logs", params=params)
            if response.status_code == 401:
                log("the hub rejected the pairing token")
                return 3
            response.raise_for_status()

            rows = sorted(response.json() or [], key=lambda r: int(r.get("id") or 0))
            for row in rows:
                after_id = max(after_id or 0, int(row.get("id") or 0))
                emit(row)
            limit = 500
            time.sleep(LAN_POLL_SECONDS)


# ----------------- Rungs 2 and 3: iroh -----------------

async def stream_over_iroh(ticket: str, token: Optional[str], agent: str, backlog: int) -> int:
    try:
        import iroh
    except ImportError:
        log("the iroh package is not installed, run: pip install iroh==1.1.0")
        return 2

    try:
        endpoint_ticket = iroh.EndpointTicket.from_string(ticket)
    except Exception as exc:
        raise SystemExit(f"that is not a readable iroh ticket: {exc}")

    addr = endpoint_ticket.endpoint_addr()
    endpoint = await iroh.Endpoint.bind(iroh.EndpointOptions(preset=iroh.preset_n0()))
    try:
        conn = await endpoint.connect(addr, IROH_ALPN)
        log(f"resolved transport: {classify(conn)} (node {conn.remote_id().fmt_short()})")

        stream = await conn.open_bi()
        send, recv = stream.send(), stream.recv()
        request = {"token": token, "agent": agent, "backlog": backlog}
        await send.write_all(json.dumps(request).encode("utf-8"))
        await send.finish()

        buffer = b""
        while True:
            chunk = await recv.read(64 * 1024)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if handle_frame(line) is False:
                    return 3
        return 0
    finally:
        await endpoint.close()


def classify(conn: Any) -> str:
    """Name the rung an established iroh connection landed on."""
    try:
        paths = conn.paths()
    except Exception:
        return "iroh-relay"
    selected = [p for p in paths if getattr(p, "is_selected", False)] or list(paths)
    for path in selected:
        if getattr(path, "is_ip", False) and not getattr(path, "is_relay", False):
            return "iroh-direct"
    return "iroh-relay"


def handle_frame(line: bytes) -> bool:
    """Print one protocol frame. Returns False when the hub refused us."""
    text = line.strip()
    if not text:
        return True
    try:
        frame = json.loads(text.decode("utf-8"))
    except Exception:
        log(f"skipping an unreadable frame: {text[:120]!r}")
        return True

    kind = frame.get("type")
    if kind == "log":
        emit(frame)
    elif kind == "hello":
        log(f"connected to {frame.get('hostname')} over {frame.get('transport')}")
    elif kind == "ping":
        pass
    elif kind == "error":
        log(f"the hub refused the connection: {frame.get('detail')}")
        return False
    return True


def emit(row: Dict[str, Any]) -> None:
    agent = row.get("agent") or "hub"
    source = row.get("source") or ""
    stamp = (row.get("timestamp") or "")[:19]
    content = (row.get("content") or "").rstrip("\n")
    print(f"[{stamp}] {agent}/{source}: {content}", flush=True)


# ----------------- Entry point -----------------

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="iroh-client.py",
        description="Stream an AgnView hub's console over LAN or iroh."
    )
    parser.add_argument("source", nargs="?", help="pairing payload JSON, an agnview:// URI, or a bare iroh ticket")
    parser.add_argument("--payload", help="same as the positional argument, for shells that dislike braces")
    parser.add_argument("--token", help="pairing token, when the source does not carry one")
    parser.add_argument("--agent", default="all", help="console filter (default: all)")
    parser.add_argument("--backlog", type=int, default=200, help="lines of history to replay (default: 200)")
    parser.add_argument(
        "--transport",
        choices=("auto", "lan", "iroh"),
        default="auto",
        help="auto follows the documented connection order (default), the others force one rung"
    )
    args = parser.parse_args()

    source = parse_source(args.payload or args.source or "")
    token = args.token or source.get("token")
    ticket = source.get("ticket")
    lan = source.get("lan")

    if args.transport in ("auto", "lan"):
        if lan_reachable(lan):
            return stream_over_lan(lan, token, args.agent, args.backlog)
        if args.transport == "lan":
            log(f"the LAN address {lan} did not answer inside {LAN_CONNECT_TIMEOUT_SECONDS}s")
            return 4
        if lan and lan.rpartition(":")[0] in ("127.0.0.1", "localhost", "::1", "[::1]"):
            log("the hub listens on its own machine only, so there is no LAN rung, trying iroh")
        else:
            log(f"the LAN rung did not answer inside {LAN_CONNECT_TIMEOUT_SECONDS}s, trying iroh")

    if not ticket:
        log("this payload carries no iroh ticket, so only the LAN rung exists")
        return 4

    try:
        return asyncio.run(stream_over_iroh(ticket, token, args.agent, args.backlog))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
