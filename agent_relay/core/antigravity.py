"""Read AntiGravity's usage panel from the app's own local debug port.

AntiGravity is an Electron app. Its packaged build hides the DevTools menu, but
``dist/main.js`` still appends ``remote-debugging-port=0`` on every run, so
Chromium listens on an OS-assigned loopback port and writes that port to the
first line of the user-data directory's ``DevToolsActivePort`` file. This module
talks to that port the way any DevTools front end would: an HTTP ``GET
/json/list`` to find the window, then one ``Runtime.evaluate`` over the
DevTools Protocol websocket to read the text already on screen.

What this is and is not:

* It is the app's own debug interface, on loopback, describing the app's own
  window. It reads text that is already displayed to the person sitting there.
* It is not credential extraction. Nothing here reads a cookie store, a browser
  profile, a token or any other stored secret, and nothing here starts an
  application. If AntiGravity is not running, this module says so and stops.
  Launching it to take a reading would be intrusive, so it never happens.

Where the parsing rules live
----------------------------
The panel wording is parsed once, here, in :func:`parse_usage_panel`. The Usage
tab still hands out a JavaScript extractor to paste into AntiGravity's console
(the fallback for someone who wants a figure another way), and that script
carries the same rules in JavaScript. Two copies of a rule drift apart, so
``tests/test_antigravity_cdp.py`` runs the real pasted script in Node and this
Python parser over the same panel fixtures and fails if the two payloads differ.
The alternative, shipping the JavaScript over ``Runtime.evaluate``, was not
chosen: the numbers would then be computed inside a window this code does not
control, and the result could not be unit-tested without a live app.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import socket
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

# Loopback only. The debug port is bound to the local machine and nothing here
# ever reaches for a remote host.
_HOST = "127.0.0.1"

# A usage refresh must not stall the dashboard, so every step is short. The app
# is on this machine: if it does not answer in a second or two, it is not going
# to.
_CONNECT_TIMEOUT = 1.0
_HTTP_TIMEOUT = 2.0
_WS_TIMEOUT = 4.0

# An innerText read of one window is tens of kilobytes. This cap stops a
# malformed or hostile frame length from asking for an unbounded allocation.
_MAX_FRAME_BYTES = 8 * 1024 * 1024

# How many page targets to try before giving up. An Electron app lists a handful
# (the window, any webview), and the usage panel lives in whichever one is
# showing the model picker.
_MAX_TARGETS = 8

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# The messages the dashboard shows. They name the one thing the person can do
# about it, which a generic "unavailable" never did.
NOT_RUNNING_MESSAGE = (
    "AntiGravity needs to be running for an automatic usage read. Open "
    "AntiGravity, or use the manual sync script to post a figure without it."
)
PANEL_NOT_ON_SCREEN_MESSAGE = (
    "AntiGravity is running, but its usage panel is not on screen. Open the "
    "model picker so the limits are showing, then refresh."
)


@dataclass
class AntigravityRead:
    """One attempt at an automatic read: a payload, or the reason there is none."""

    payload: Optional[dict] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Is AntiGravity running, and where is its debug port
# ---------------------------------------------------------------------------


def devtools_port_file() -> Path:
    """The DevToolsActivePort file for this platform's AntiGravity install.

    Only the well-known per-platform user-data location is used, so this works
    for any install of the app rather than one machine's paths.
    """
    if sys.platform.startswith("win"):
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return root / "Antigravity" / "DevToolsActivePort"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Antigravity"
            / "DevToolsActivePort"
        )
    config = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config) if config else Path.home() / ".config"
    return root / "Antigravity" / "DevToolsActivePort"


def read_devtools_port(port_file: Optional[Path] = None) -> Optional[int]:
    """The port on the first line of the file, or None when there is no usable one."""
    path = port_file or devtools_port_file()
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    try:
        port = int(first_line.strip())
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def port_is_listening(port: int, host: str = _HOST, timeout: float = _CONNECT_TIMEOUT) -> bool:
    """True when something on this machine is accepting connections on that port.

    This is the process check. A name check alone would be weaker: the port file
    outlives the app, so a closed AntiGravity leaves a file naming a port that
    nothing is listening on, and only a connection attempt tells the two apart.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def is_running(port_file: Optional[Path] = None) -> bool:
    """True when AntiGravity has a debug port open on this machine right now."""
    port = read_devtools_port(port_file)
    return port is not None and port_is_listening(port)


# ---------------------------------------------------------------------------
# A minimal websocket client (RFC 6455) for the DevTools Protocol
# ---------------------------------------------------------------------------
#
# The DevTools endpoint is plain ws:// on loopback, with no compression and no
# subprotocol, so the handful of frames needed here fit in the standard library
# and the app gains no dependency for it.


class _WebSocket:
    """One client connection: handshake, one text frame out, text frames in."""

    def __init__(self, host: str, port: int, path: str, timeout: float = _WS_TIMEOUT):
        self._buf = b""
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(timeout)
        try:
            self._handshake(host, port, path)
        except Exception:
            self.close()
            raise

    def _handshake(self, host: str, port: int, path: str) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self._sock.sendall(request.encode("ascii"))

        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("the debug port closed the connection during the handshake")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise ConnectionError("the debug port sent an oversized handshake response")

        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        lines = head.decode("latin-1").split("\r\n")
        if "101" not in lines[0].split(" ")[:2]:
            raise ConnectionError(f"the debug port refused the upgrade: {lines[0]}")

        expected = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()).decode(
            "ascii"
        )
        accepted = None
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accepted = value.strip()
        if accepted != expected:
            raise ConnectionError("the debug port returned a bad websocket accept header")

    def _read_exactly(self, count: int) -> bytes:
        while len(self._buf) < count:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("the debug port closed the connection")
            self._buf += chunk
        taken, self._buf = self._buf[:count], self._buf[count:]
        return taken

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", length)
        # A client must mask every frame it sends.
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self) -> str:
        message = bytearray()
        while True:
            first, second = self._read_exactly(2)
            fin = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read_exactly(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read_exactly(8))
            if length > _MAX_FRAME_BYTES:
                raise ConnectionError("the debug port sent an oversized frame")
            mask = self._read_exactly(4) if masked else b""
            data = self._read_exactly(length)
            if mask:
                data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))

            if opcode == 0x8:
                raise ConnectionError("the debug port closed the websocket")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:  # pong
                continue
            message += data
            if fin:
                return message.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "_WebSocket":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# The DevTools Protocol call
# ---------------------------------------------------------------------------

# Read the text the window is already showing. Nothing is clicked, typed or
# navigated, and no other domain is enabled.
_PANEL_TEXT_EXPRESSION = "(document.body && document.body.innerText) || ''"


def list_page_targets(port: int, timeout: float = _HTTP_TIMEOUT) -> list:
    """The debuggable page targets the app publishes, most useful first."""
    with httpx.Client(timeout=timeout) as client:
        res = client.get(f"http://{_HOST}:{port}/json/list")
    res.raise_for_status()
    targets = res.json()
    if not isinstance(targets, list):
        return []
    pages = [
        target
        for target in targets
        if isinstance(target, dict)
        and target.get("type") == "page"
        and isinstance(target.get("webSocketDebuggerUrl"), str)
        and not str(target.get("url", "")).startswith("devtools://")
    ]
    return pages[:_MAX_TARGETS]


def _evaluate(ws_url: str, expression: str, timeout: float = _WS_TIMEOUT) -> Optional[str]:
    """Run one expression in a target and return its string result."""
    host, port, path = _split_ws_url(ws_url)
    with _WebSocket(host, port, path, timeout=timeout) as ws:
        ws.send_text(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": False,
                    },
                }
            )
        )
        # The target may push events before the reply, so read until the id
        # asked for comes back.
        for _ in range(50):
            message = json.loads(ws.recv_text())
            if message.get("id") != 1:
                continue
            result = (message.get("result") or {}).get("result") or {}
            value = result.get("value")
            return value if isinstance(value, str) else None
    return None


def _split_ws_url(ws_url: str) -> tuple:
    """Pull host, port and path out of a ws:// URL without a URL library detour."""
    rest = ws_url.split("://", 1)[1] if "://" in ws_url else ws_url
    authority, _, path = rest.partition("/")
    host, _, port_text = authority.partition(":")
    return host or _HOST, int(port_text or 80), "/" + path


# ---------------------------------------------------------------------------
# The panel parsing rules (the single copy)
# ---------------------------------------------------------------------------

# A group heading names a family of models, for example "Gemini Models" or
# "Claude and GPT models". Every limit line under it belongs to that group until
# the next heading, so both groups are read, not just the first.
_GROUP_HEADING = re.compile(r"\bmodels?\s*$", re.IGNORECASE)
# The wording says whether the number is what is left or what is spent. A line
# that says neither is skipped rather than read the wrong way round.
_LIMIT_LINE = re.compile(
    r"^(five[\s-]?hour|5[\s-]?hour|weekly)\s+limit(?:\s+(remaining|left|used))?\b",
    re.IGNORECASE,
)
_PERCENT_ON_LINE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_PERCENT_ALONE = re.compile(r"^(\d+(?:\.\d+)?)\s*%$")

# "Resets in 2d 14h", "Resets in 3h 47m". Carried as an offset in seconds, not
# as an instant: the JavaScript extractor parses the same line, and a test
# compares the two payloads for equality, so both have to produce the same
# value without depending on when each one ran. The offset is anchored to an
# absolute instant by whoever receives it.
_RESET_LINE = re.compile(r"\bresets?\s+in\b", re.IGNORECASE)
_RESET_PARTS = re.compile(r"(\d+)\s*([dhm])", re.IGNORECASE)


def _reset_seconds(line: str):
    """Seconds until a reset, from the panel's own wording. None when absent."""
    if not _RESET_LINE.search(line or ""):
        return None
    total = 0
    matched = False
    for amount, unit in _RESET_PARTS.findall(line):
        unit = unit.lower()
        total += int(amount) * {"d": 86400, "h": 3600, "m": 60}[unit]
        matched = True
    return total if matched else None


def _round1(value: float) -> float:
    """Round half up to one decimal, the way the JavaScript extractor does."""
    return math.floor(value * 10 + 0.5) / 10


def _is_group_heading(line: str) -> bool:
    return (
        bool(_GROUP_HEADING.search(line))
        and "%" not in line
        and not re.search(r"limit", line, re.IGNORECASE)
        and len(line) <= 60
    )


def parse_usage_panel(panel_text: str) -> dict:
    """Turn the model picker's own text into the telemetry payload.

    Only what the window actually said. A group or a window that could not be
    read is left out, so a failed read reports nothing rather than a guess. An
    empty dict means there was nothing readable on screen.
    """
    lines = [line.strip() for line in (panel_text or "").split("\n")]
    lines = [line for line in lines if line]

    groups: list = []
    current: Optional[dict] = None
    for index, line in enumerate(lines):
        if _is_group_heading(line):
            current = {"group": line, "windows": {}}
            groups.append(current)
            continue
        limit_match = _LIMIT_LINE.match(line)
        if not limit_match or current is None:
            continue
        sense = (limit_match.group(2) or "").lower()
        if not sense:
            continue
        # The figure sits on the label's own line, or on one of the next two
        # lines when the panel puts it underneath.
        percent_match = _PERCENT_ON_LINE.search(line)
        lookahead = index + 1
        while percent_match is None and lookahead < len(lines) and lookahead <= index + 2:
            percent_match = _PERCENT_ALONE.match(lines[lookahead])
            lookahead += 1
        if percent_match is None:
            continue
        value = float(percent_match.group(1))
        used = value if sense == "used" else _round1(100 - value)
        left = _round1(100 - value) if sense == "used" else value
        key = "weekly" if re.search(r"weekly", limit_match.group(1), re.IGNORECASE) else "session"
        # The reset sits between the label and the figure when the panel shows
        # one at all. Only the Gemini group carries one on some builds, so its
        # absence is normal and leaves the window with no countdown.
        resets_in = _reset_seconds(line)
        scan = index + 1
        while resets_in is None and scan < len(lines) and scan <= index + 2:
            resets_in = _reset_seconds(lines[scan])
            scan += 1
        current["windows"][key] = {
            "title": _PERCENT_ON_LINE.sub("", line, count=1).strip(),
            "used": used,
            "left": left,
            "resets_in_seconds": resets_in,
        }

    weekly_rows = []
    session_rows = []
    for group in groups:
        weekly = group["windows"].get("weekly")
        if weekly:
            weekly_rows.append(
                {
                    "group": group["group"],
                    "weekly_title": weekly["title"],
                    "weekly_percent_used": weekly["used"],
                    "weekly_percent_left": weekly["left"],
                    "weekly_resets_in_seconds": weekly.get("resets_in_seconds"),
                }
            )
        session = group["windows"].get("session")
        if session:
            session_rows.append(
                {
                    "group": group["group"],
                    "session_title": session["title"],
                    "session_percent_used": session["used"],
                    "session_percent_left": session["left"],
                    "session_resets_in_seconds": session.get("resets_in_seconds"),
                }
            )

    if not weekly_rows and not session_rows:
        return {}

    payload: dict = {"provider": "gemini"}
    if weekly_rows:
        payload["weekly_breakdown"] = weekly_rows
        # The headline figure is the group closest to its limit, so the single
        # number on the card is the one that actually constrains the account. It
        # is picked from the rows above, never computed out of nothing.
        tightest = _tightest(weekly_rows, "weekly_percent_used")
        payload["weekly_title"] = tightest["weekly_title"]
        payload["weekly_percent_used"] = tightest["weekly_percent_used"]
        payload["weekly_percent_left"] = tightest["weekly_percent_left"]
        payload["weekly_resets_in_seconds"] = tightest.get("weekly_resets_in_seconds")
    if session_rows:
        payload["session_breakdown"] = session_rows
        tightest = _tightest(session_rows, "session_percent_used")
        payload["session_title"] = tightest["session_title"]
        payload["session_percent_used"] = tightest["session_percent_used"]
        payload["session_percent_left"] = tightest["session_percent_left"]
        payload["session_resets_in_seconds"] = tightest.get("session_resets_in_seconds")
    return payload


def _tightest(rows: list, field: str) -> dict:
    """The row closest to its limit, keeping the first on a tie, as the script does."""
    best = rows[0]
    for row in rows[1:]:
        if row[field] > best[field]:
            best = row
    return best


# ---------------------------------------------------------------------------
# The whole read
# ---------------------------------------------------------------------------


def read_usage(port_file: Optional[Path] = None) -> AntigravityRead:
    """Read AntiGravity's usage panel, or say why there is nothing to read.

    This never starts AntiGravity and never raises: a usage refresh that cannot
    read a figure must leave the dashboard honest, not broken.
    """
    port = read_devtools_port(port_file)
    if port is None or not port_is_listening(port):
        # A port file left behind by a closed app names a port nothing answers
        # on, so both cases are the same thing: the app is not running.
        return AntigravityRead(error=NOT_RUNNING_MESSAGE)

    try:
        targets = list_page_targets(port)
    except Exception as exc:
        return AntigravityRead(
            error=(
                f"AntiGravity is running, but its debug port did not list a window "
                f"({exc}). Use the manual sync script for a figure."
            )
        )

    if not targets:
        return AntigravityRead(
            error=(
                "AntiGravity is running, but its debug port listed no window to "
                "read. Use the manual sync script for a figure."
            )
        )

    last_error: Optional[Exception] = None
    for target in targets:
        try:
            panel_text = _evaluate(target["webSocketDebuggerUrl"], _PANEL_TEXT_EXPRESSION)
        except Exception as exc:
            last_error = exc
            continue
        if not panel_text:
            continue
        payload = parse_usage_panel(panel_text)
        if payload:
            return AntigravityRead(payload=payload)

    if last_error is not None:
        return AntigravityRead(
            error=(
                f"AntiGravity is running, but reading its window failed "
                f"({last_error}). Use the manual sync script for a figure."
            )
        )
    return AntigravityRead(error=PANEL_NOT_ON_SCREEN_MESSAGE)
