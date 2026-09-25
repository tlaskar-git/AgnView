"""The mobile API over iroh: one allowlisted request per stream, served in-process.

A phone that reaches the hub over iroh sends one JSON request per stream and
gets one response frame back. The request is never answered here. It is handed
to the hub's own FastAPI app in-process, with the pairing key in the same
header a LAN client sends, so the auth middleware, the request models and the
route handlers behave exactly as they do on the LAN. Nothing opens a second
network hop.

Only the requests in API_ALLOWLIST get that far. Everything else is refused
before the app sees it.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# The value of the request's "op" key that selects API mode. A request with no
# "op" key is a console request, exactly as before this mode existed.
API_OP = "api"

# The only requests a phone may make over iroh: (method, path template, query
# allowed). {id} stands for one path segment of letters, digits, "_", "-" and
# ".", never "." or "..". These are the routes the iOS app needs for Usage,
# Pipelines, Sessions and prompt dispatch, all of which the LAN already serves
# to a paired phone. Pairing, settings and anything that changes the hub
# itself stay LAN only. Add a route here only when the app needs it.
API_ALLOWLIST: Tuple[Tuple[str, str, bool], ...] = (
    ("GET", "/api/mobile/status", False),
    ("GET", "/api/usage/accounts", False),
    ("GET", "/api/jobs", False),
    ("GET", "/api/jobs/{id}", False),
    ("GET", "/api/console/live-sessions", False),
    ("GET", "/api/console/logs", True),
    ("POST", "/api/console/dispatch", False),
    ("POST", "/api/tasks/{id}/request-revision", False),
    ("POST", "/api/tasks/{id}/fail", False),
)

# Query keys GET /api/console/logs accepts. A query is passed through only when
# every key is one of these.
ALLOWED_QUERY_KEYS = frozenset({"agent", "limit", "session_id", "after_id"})

# The largest response body a phone is sent, before JSON framing.
MAX_RESPONSE_BYTES = 1024 * 1024

# How long one API request may take from the moment it is authorised.
API_TIMEOUT_SECONDS = 30.0

# How many API requests may be in flight at once. The console stream is not
# counted, so a burst of API calls can never starve it.
MAX_API_CALLS_PER_CONNECTION = 4
MAX_API_CALLS_TOTAL = 16

# The client address the in-process request carries. It is not a loopback
# address, so nothing in the app mistakes a phone on iroh for a local browser.
IROH_CLIENT_ADDRESS = ("iroh", 0)

# Error codes an API request can end with, in the existing error frame.
ERROR_UNAUTHORISED = "unauthorised"
ERROR_FORBIDDEN_PATH = "forbidden_path"
ERROR_BAD_REQUEST = "bad_request"
ERROR_TOO_LARGE = "too_large"
ERROR_TIMEOUT = "timeout"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_UPSTREAM = "upstream_error"

_SEGMENT = r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}"
_PATH_CHARS = re.compile(r"^[A-Za-z0-9/._-]+$")
_QUERY_CHARS = re.compile(r"^[A-Za-z0-9._~=&@:-]*$")


def _compile(template: str) -> "re.Pattern[str]":
    return re.compile("^" + re.escape(template).replace(re.escape("{id}"), _SEGMENT) + "$")


_COMPILED_ALLOWLIST = tuple(
    (method, template, query_allowed, _compile(template))
    for method, template, query_allowed in API_ALLOWLIST
)


class ApiError(Exception):
    """An API request that ends in an error frame with this code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass
class ApiRequest:
    """A request that passed the allowlist, ready to hand to the app."""

    method: str
    path: str
    query: str
    template: str
    body: Optional[bytes]


def match_allowlist(method: Any, target: Any) -> Tuple[str, str, str]:
    """Return (path, query, template) for an allowlisted request.

    Raises ApiError(forbidden_path) for anything outside the allowlist: a
    traversal, a percent-encoded character, a scheme or host, a doubled slash,
    a query where the route takes none, or a method the route does not serve.
    """
    if not isinstance(method, str) or not isinstance(target, str):
        raise ApiError(ERROR_BAD_REQUEST)

    path, sep, query = target.partition("?")
    if not path.startswith("/api/") or "//" in path or ".." in path:
        raise ApiError(ERROR_FORBIDDEN_PATH)
    if not _PATH_CHARS.match(path):
        # Rules out "%" (encoded slashes and dots), "\\", ":" (a scheme), "#"
        # and whitespace in one check.
        raise ApiError(ERROR_FORBIDDEN_PATH)

    for allowed_method, template, query_allowed, pattern in _COMPILED_ALLOWLIST:
        if allowed_method != method or not pattern.match(path):
            continue
        if sep and not query_allowed:
            raise ApiError(ERROR_FORBIDDEN_PATH)
        if query and not _query_is_allowed(query):
            raise ApiError(ERROR_FORBIDDEN_PATH)
        return path, query, template
    raise ApiError(ERROR_FORBIDDEN_PATH)


def _query_is_allowed(query: str) -> bool:
    if not _QUERY_CHARS.match(query):
        return False
    seen = set()
    for pair in query.split("&"):
        key, _, _ = pair.partition("=")
        if key not in ALLOWED_QUERY_KEYS or key in seen:
            return False
        seen.add(key)
    return True


def parse_api_request(request: Dict[str, Any]) -> ApiRequest:
    """Validate an API-mode request object. Raises ApiError on any problem."""
    method = request.get("method")
    target = request.get("path")
    path, query, template = match_allowlist(method, target)

    body = request.get("body")
    if method == "GET":
        if body is not None:
            raise ApiError(ERROR_BAD_REQUEST)
        encoded: Optional[bytes] = None
    else:
        encoded = None if body is None else json.dumps(body).encode("utf-8")
    return ApiRequest(method=method, path=path, query=query, template=template, body=encoded)


class ApiSlots:
    """Counts API requests in flight, per connection and across the hub."""

    def __init__(self, limit: int):
        self.limit = limit
        self.in_flight = 0

    def full(self) -> bool:
        return self.in_flight >= self.limit


class _Claim:
    """Hold one slot in every counter for the length of a request."""

    def __init__(self, *slots: ApiSlots):
        self._slots = slots

    def __enter__(self) -> "_Claim":
        if any(slot.full() for slot in self._slots):
            raise ApiError(ERROR_RATE_LIMITED)
        for slot in self._slots:
            slot.in_flight += 1
        return self

    def __exit__(self, *exc_info: Any) -> None:
        for slot in self._slots:
            slot.in_flight -= 1


def claim(*slots: ApiSlots) -> _Claim:
    return _Claim(*slots)


async def forward_to_app(
    app: Any,
    request: ApiRequest,
    token: Optional[str],
    timeout: Optional[float] = None,
    max_body: Optional[int] = None,
) -> Dict[str, Any]:
    """Run one request through the hub's ASGI app and return the response frame.

    The request goes through the whole app, middleware included, with the
    pairing key in X-AgnView-Token as a LAN client sends it. Raises ApiError
    with timeout, too_large or upstream_error.
    """
    limit = MAX_RESPONSE_BYTES if max_body is None else max_body
    body = request.body or b""
    headers: List[Tuple[bytes, bytes]] = [
        (b"host", b"iroh"),
        (b"user-agent", b"agnview-iroh"),
        (b"accept", b"application/json"),
    ]
    if token:
        headers.append((b"x-agnview-token", token.encode("utf-8")))
    if request.body is not None:
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(body)).encode("ascii")))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": "http",
        "path": request.path,
        "raw_path": request.path.encode("ascii"),
        "query_string": request.query.encode("ascii"),
        "root_path": "",
        "headers": headers,
        "client": IROH_CLIENT_ADDRESS,
        "server": ("iroh", 0),
        "extensions": {},
    }

    response: Dict[str, Any] = {"status": None, "headers": [], "size": 0, "chunks": []}
    finished = asyncio.Event()
    body_sent = False

    async def receive() -> Dict[str, Any]:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: Dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "http.response.start":
            response["status"] = int(message["status"])
            response["headers"] = list(message.get("headers") or [])
        elif kind == "http.response.body":
            chunk = message.get("body") or b""
            response["size"] += len(chunk)
            # Keep counting past the limit, but stop holding the bytes.
            if response["size"] <= limit:
                response["chunks"].append(chunk)
            if not message.get("more_body", False):
                finished.set()

    try:
        await asyncio.wait_for(
            app(scope, receive, send),
            timeout=API_TIMEOUT_SECONDS if timeout is None else timeout,
        )
    except asyncio.TimeoutError:
        raise ApiError(ERROR_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ApiError(ERROR_UPSTREAM)
    finally:
        finished.set()

    if response["status"] is None:
        raise ApiError(ERROR_UPSTREAM)
    if response["size"] > limit:
        raise ApiError(ERROR_TOO_LARGE)

    raw = b"".join(response["chunks"])
    return {
        "type": "response",
        "status": response["status"],
        "body": _decode_body(raw, response["headers"]),
    }


def _decode_body(raw: bytes, headers: List[Tuple[bytes, bytes]]) -> Any:
    """Return JSON as a value and anything else as text. An empty body is null."""
    if not raw:
        return None
    content_type = ""
    for name, value in headers:
        if name.lower() == b"content-type":
            content_type = value.decode("latin-1").lower()
            break
    text = raw.decode("utf-8", errors="replace")
    if "json" in content_type:
        try:
            return json.loads(text)
        except ValueError:
            pass
    return text
