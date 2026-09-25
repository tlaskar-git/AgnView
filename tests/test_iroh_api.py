"""The mobile API over iroh: allowlist, limits, auth and in-process forwarding.

These tests drive the stream handler with in-memory stand-ins for iroh's send
and receive streams, so they need no network. tests/test_iroh_api_live.py runs
the same protocol over real iroh endpoints on the loopback interface.
"""

import asyncio
import json
import secrets

import pytest

from agent_relay.api.app import create_app
from agent_relay.core import iroh_api
from agent_relay.core import iroh_transport as transport_module
from agent_relay.core.iroh_api import (
    ApiError,
    ApiRequest,
    forward_to_app,
    match_allowlist,
    parse_api_request,
)
from agent_relay.core.iroh_transport import IROH_RATE_LIMIT_KEY, IrohTransport
from agent_relay.core.pairing import record_failed_auth, reset_auth_rate_limit


def _fake_token() -> str:
    # Generated per run. No real pairing key ever appears in the suite.
    return secrets.token_urlsafe(32)


@pytest.fixture(autouse=True)
def clean_rate_limit():
    keys = (IROH_RATE_LIMIT_KEY, f"{IROH_RATE_LIMIT_KEY}:unknown")
    for key in keys:
        reset_auth_rate_limit(key)
    yield
    for key in keys:
        reset_auth_rate_limit(key)


# ----------------- In-memory streams -----------------

class FakeRecv:
    def __init__(self, data: bytes, hang: bool = False):
        self._data = data
        self._hang = hang
        self.stopped_with = None

    async def read(self, size_limit: int) -> bytes:
        if self._hang:
            await asyncio.sleep(3600)
        chunk, self._data = self._data[:size_limit], self._data[size_limit:]
        return chunk

    async def stop(self, code: int) -> None:
        self.stopped_with = code


class FakeSend:
    def __init__(self):
        self.buffer = b""
        self.finished = False

    async def write_all(self, data: bytes) -> None:
        self.buffer += data

    async def finish(self) -> None:
        self.finished = True

    async def stopped(self):
        return None

    def frames(self):
        return [json.loads(line) for line in self.buffer.decode("utf-8").splitlines() if line.strip()]


class FakeBi:
    def __init__(self, request, hang: bool = False):
        if isinstance(request, (dict, list)):
            raw = (json.dumps(request) + "\n").encode("utf-8")
        else:
            raw = request
        self._recv = FakeRecv(raw, hang=hang)
        self._send = FakeSend()

    def recv(self):
        return self._recv

    def send(self):
        return self._send


class FakeConn:
    def __init__(self):
        self.closed = False

    def paths(self):
        return []

    def close(self, code, reason):
        self.closed = True


def _serve(transport, request, hang=False, slots=None):
    bi = FakeBi(request, hang=hang)
    conn = FakeConn()
    slots = slots or iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_PER_CONNECTION)
    asyncio.run(transport._handle_stream(conn, bi, slots))
    return bi._send.frames(), conn, bi


# ----------------- A tiny ASGI app standing in for the hub -----------------

class EchoApp:
    """Answers every request with what it received, as JSON."""

    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        message = await receive()
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        self.calls.append((scope["method"], scope["path"], scope["query_string"], headers, message.get("body")))
        payload = json.dumps({
            "method": scope["method"],
            "path": scope["path"],
            "query": scope["query_string"].decode(),
            "client": list(scope["client"]),
            "body": (message.get("body") or b"").decode() or None,
        }).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})


def _transport(token, app=None, api_enabled=True):
    return IrohTransport(
        token_provider=lambda: token,
        asgi_app=app if app is not None else EchoApp(),
        api_enabled=api_enabled,
    )


def _api(token, method="GET", path="/api/mobile/status", body=None):
    return {"token": token, "op": "api", "method": method, "path": path, "body": body}


# ----------------- Allowlist and path normalisation -----------------

@pytest.mark.parametrize("method,target,template", [
    ("GET", "/api/mobile/status", "/api/mobile/status"),
    ("GET", "/api/usage/accounts", "/api/usage/accounts"),
    ("GET", "/api/jobs", "/api/jobs"),
    ("GET", "/api/jobs/job-1a2b3c4d", "/api/jobs/{id}"),
    ("GET", "/api/console/live-sessions", "/api/console/live-sessions"),
    ("GET", "/api/console/logs", "/api/console/logs"),
    ("GET", "/api/console/logs?agent=all&limit=50&after_id=12", "/api/console/logs"),
    ("POST", "/api/console/dispatch", "/api/console/dispatch"),
    ("POST", "/api/tasks/t1/request-revision", "/api/tasks/{id}/request-revision"),
    ("POST", "/api/tasks/task_2.v1/fail", "/api/tasks/{id}/fail"),
])
def test_allowlisted_requests_pass(method, target, template):
    path, query, matched = match_allowlist(method, target)
    assert matched == template
    assert path == target.partition("?")[0]
    assert query == target.partition("?")[2]


@pytest.mark.parametrize("target", [
    "/api/jobs/../mobile/pairing",
    "/api/jobs/..",
    "/api/jobs/.",
    "/api/../api/jobs",
    "/api/jobs/job-1%2F..%2Fx",
    "/api/jobs%2Fjob-1",
    "/api/jobs/%2e%2e",
    "/api//jobs",
    "//evil.example/api/jobs",
    "http://evil.example/api/jobs",
    "https://127.0.0.1:8765/api/jobs",
    "api/jobs",
    "/api/jobs\\..\\mobile",
    "/api/jobs/a b",
    "/api/jobs/job-1#frag",
    "/api/mobile/pairing",
    "/api/mobile/pairing/regenerate",
    "/api/system/lan",
    "/api/config/relay",
    "/api/events",
    "/",
    "",
])
def test_anything_off_the_allowlist_is_forbidden(target):
    with pytest.raises(ApiError) as caught:
        match_allowlist("GET", target)
    assert caught.value.code == "forbidden_path"


@pytest.mark.parametrize("method,target", [
    ("POST", "/api/jobs"),
    ("DELETE", "/api/jobs/job-1"),
    ("GET", "/api/console/dispatch"),
    ("PUT", "/api/usage/accounts"),
    ("get", "/api/jobs"),
    ("POST", "/api/tasks/t1/complete"),
])
def test_a_method_the_route_does_not_serve_is_forbidden(method, target):
    with pytest.raises(ApiError) as caught:
        match_allowlist(method, target)
    assert caught.value.code == "forbidden_path"


@pytest.mark.parametrize("target", [
    "/api/console/dispatch?agent=codex",
    "/api/console/dispatch?",
    "/api/jobs?limit=1",
    "/api/console/logs?token=abc",
    "/api/console/logs?agent=all&agent=codex",
    "/api/console/logs?agent=%2e%2e",
    "/api/console/logs?next=http://evil.example",
])
def test_queries_are_refused_except_known_keys_on_the_logs_route(target):
    method = "POST" if "dispatch" in target else "GET"
    with pytest.raises(ApiError) as caught:
        match_allowlist(method, target)
    assert caught.value.code == "forbidden_path"


def test_a_request_that_is_not_strings_is_a_bad_request():
    for method, target in ((None, "/api/jobs"), ("GET", None), ("GET", 7), (["GET"], "/api/jobs")):
        with pytest.raises(ApiError) as caught:
            match_allowlist(method, target)
        assert caught.value.code == "bad_request"


def test_a_get_with_a_body_is_a_bad_request():
    with pytest.raises(ApiError) as caught:
        parse_api_request({"op": "api", "method": "GET", "path": "/api/jobs", "body": {"x": 1}})
    assert caught.value.code == "bad_request"


def test_a_post_body_is_sent_as_json():
    request = parse_api_request({
        "op": "api", "method": "POST", "path": "/api/console/dispatch",
        "body": {"agent": "codex", "prompt": "hello"},
    })
    assert json.loads(request.body) == {"agent": "codex", "prompt": "hello"}
    assert request.template == "/api/console/dispatch"


def test_the_allowlist_is_one_constant_and_serves_real_routes(tmp_path):
    app = create_app(db_path=str(tmp_path / "hub.db"))
    served = app.openapi()["paths"]
    for method, template, _query in iroh_api.API_ALLOWLIST:
        openapi_path = template
        for name in ("{job_id}", "{task_id}", "{upload_id}"):
            candidate = template.replace("{id}", name)
            if candidate in served:
                openapi_path = candidate
        assert openapi_path in served, template
        assert method.lower() in served[openapi_path], (method, template)


# ----------------- Forwarding to an ASGI app -----------------

def test_forwarding_reaches_the_app_with_the_lan_header():
    app = EchoApp()
    token = _fake_token()
    request = ApiRequest(method="POST", path="/api/console/dispatch", query="",
                         template="/api/console/dispatch", body=b'{"a": 1}')
    frame = asyncio.run(forward_to_app(app, request, token=token))

    assert frame["type"] == "response"
    assert frame["status"] == 200
    assert frame["body"]["method"] == "POST"
    assert frame["body"]["path"] == "/api/console/dispatch"
    assert frame["body"]["body"] == '{"a": 1}'
    # Not a loopback address, so the app never takes a phone for a local browser.
    assert frame["body"]["client"][0] == "iroh"
    headers = app.calls[0][3]
    assert headers["x-agnview-token"] == token
    assert headers["content-type"] == "application/json"
    assert "sec-fetch-site" not in headers and "referer" not in headers


def test_a_text_response_comes_back_as_a_string():
    async def text_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"not here"})

    request = ApiRequest("GET", "/api/jobs", "", "/api/jobs", None)
    frame = asyncio.run(forward_to_app(text_app, request, token="x"))
    assert frame == {"type": "response", "status": 404, "body": "not here"}


def test_a_response_over_the_cap_is_too_large():
    async def big_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"x" * 600, "more_body": True})
        await send({"type": "http.response.body", "body": b"x" * 600})

    request = ApiRequest("GET", "/api/jobs", "", "/api/jobs", None)
    with pytest.raises(ApiError) as caught:
        asyncio.run(forward_to_app(big_app, request, token="x", max_body=1000))
    assert caught.value.code == "too_large"


def test_the_default_response_cap_is_one_mebibyte():
    assert iroh_api.MAX_RESPONSE_BYTES == 1024 * 1024
    assert iroh_api.API_TIMEOUT_SECONDS == 30.0
    assert transport_module.MAX_REQUEST_BYTES == 64 * 1024


def test_a_slow_app_times_out():
    async def slow_app(scope, receive, send):
        await asyncio.sleep(5)

    request = ApiRequest("GET", "/api/jobs", "", "/api/jobs", None)
    with pytest.raises(ApiError) as caught:
        asyncio.run(forward_to_app(slow_app, request, token="x", timeout=0.05))
    assert caught.value.code == "timeout"


def test_an_app_that_raises_is_an_upstream_error():
    async def broken_app(scope, receive, send):
        raise RuntimeError("boom")

    request = ApiRequest("GET", "/api/jobs", "", "/api/jobs", None)
    with pytest.raises(ApiError) as caught:
        asyncio.run(forward_to_app(broken_app, request, token="x"))
    assert caught.value.code == "upstream_error"


def test_the_real_app_still_applies_its_auth_middleware(tmp_path):
    token = _fake_token()
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)
    request = ApiRequest("GET", "/api/mobile/status", "", "/api/mobile/status", None)

    ok = asyncio.run(forward_to_app(app, request, token=token))
    assert ok["status"] == 200
    assert ok["body"]["status"] == "healthy"

    # The middleware is not bypassed: a wrong key gets the LAN's own 401.
    refused = asyncio.run(forward_to_app(app, request, token=_fake_token()))
    assert refused["status"] == 401
    reset_auth_rate_limit(IROH_RATE_LIMIT_KEY)


def test_the_real_app_validates_a_dispatch_as_on_the_lan(tmp_path):
    token = _fake_token()
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)
    request = parse_api_request({"op": "api", "method": "POST", "path": "/api/console/dispatch", "body": {"prompt": "x"}})
    frame = asyncio.run(forward_to_app(app, request, token=token))
    # The request model rejects a dispatch with no agent, exactly as over HTTP.
    assert frame["status"] == 422


# ----------------- The stream handler -----------------

def test_an_api_request_gets_hello_then_one_response():
    token = _fake_token()
    app = EchoApp()
    frames, conn, bi = _serve(_transport(token, app), _api(token, "GET", "/api/console/logs?agent=all&limit=5"))

    assert [f["type"] for f in frames] == ["hello", "response"]
    assert frames[0]["capabilities"] == ["console", "api"]
    assert frames[1]["status"] == 200
    assert frames[1]["body"]["query"] == "agent=all&limit=5"
    assert app.calls[0][3]["x-agnview-token"] == token
    # The stream ends, the connection stays open for the next request.
    assert bi._send.finished
    assert conn.closed is False


def test_a_wrong_key_gets_the_same_unauthorised_detail_in_both_modes():
    token = _fake_token()
    api_frames, api_conn, _ = _serve(_transport(token), _api(_fake_token()))
    console_frames, console_conn, _ = _serve(_transport(token), {"token": _fake_token(), "agent": "all"})

    assert api_frames == [{"type": "error", "detail": "unauthorised"}]
    assert console_frames == [{"type": "error", "detail": "unauthorised"}]
    assert api_conn.closed and console_conn.closed


def test_a_missing_key_is_unauthorised_and_the_app_is_never_called():
    token = _fake_token()
    app = EchoApp()
    request = _api(token)
    request.pop("token")
    frames, _, _ = _serve(_transport(token, app), request)
    assert frames == [{"type": "error", "detail": "unauthorised"}]
    assert app.calls == []


def test_api_mode_needs_a_key_even_on_a_hub_without_one():
    frames, _, _ = _serve(_transport(None), _api(None))
    assert frames == [{"type": "error", "detail": "unauthorised"}]


def test_failed_keys_share_the_lan_rate_limit():
    token = _fake_token()
    transport = _transport(token)
    for _ in range(10):
        frames, _, _ = _serve(transport, _api(_fake_token()))
        assert frames == [{"type": "error", "detail": "unauthorised"}]

    # The next wrong key is throttled. A right key is checked first and is
    # never refused, in either mode.
    frames, _, _ = _serve(transport, _api(_fake_token()))
    assert frames == [{"type": "error", "detail": "rate_limited"}]
    frames, _, _ = _serve(transport, _api(token))
    assert frames[-1]["type"] == "response"


def test_the_limiter_is_the_one_the_lan_uses():
    for _ in range(10):
        record_failed_auth(f"{IROH_RATE_LIMIT_KEY}:unknown")
    frames, _, _ = _serve(_transport(_fake_token()), _api(_fake_token()))
    assert frames == [{"type": "error", "detail": "rate_limited"}]


def test_a_forbidden_path_is_refused_after_hello():
    token = _fake_token()
    app = EchoApp()
    frames, _, _ = _serve(_transport(token, app), _api(token, "POST", "/api/mobile/pairing/regenerate"))
    assert frames[0]["type"] == "hello"
    assert frames[1] == {"type": "error", "detail": "forbidden_path"}
    assert app.calls == []


def test_an_unknown_op_is_a_bad_request():
    token = _fake_token()
    request = _api(token)
    request["op"] = "shell"
    frames, _, _ = _serve(_transport(token), request)
    assert frames[-1] == {"type": "error", "detail": "bad_request"}


def test_an_oversized_request_is_too_large():
    token = _fake_token()
    request = _api(token, "POST", "/api/console/dispatch", {"prompt": "x" * (70 * 1024)})
    frames, _, bi = _serve(_transport(token), request)
    assert frames == [{"type": "error", "detail": "too_large"}]
    assert bi._recv.stopped_with == 0


def test_a_client_that_never_finishes_sending_times_out(monkeypatch):
    monkeypatch.setattr(transport_module, "REQUEST_READ_TIMEOUT_SECONDS", 0.05)
    frames, _, _ = _serve(_transport(_fake_token()), b"", hang=True)
    assert frames == [{"type": "error", "detail": "timeout"}]


def test_a_slow_api_call_times_out(monkeypatch):
    async def slow_app(scope, receive, send):
        await asyncio.sleep(5)

    monkeypatch.setattr(iroh_api, "API_TIMEOUT_SECONDS", 0.05)
    token = _fake_token()
    frames, _, _ = _serve(_transport(token, slow_app), _api(token))
    assert frames[-1] == {"type": "error", "detail": "timeout"}


def test_a_full_connection_is_rate_limited_not_queued():
    token = _fake_token()
    slots = iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_PER_CONNECTION)
    slots.in_flight = slots.limit
    frames, _, _ = _serve(_transport(token), _api(token), slots=slots)
    assert frames[-1] == {"type": "error", "detail": "rate_limited"}


def test_a_full_hub_is_rate_limited_and_slots_are_released():
    token = _fake_token()
    transport = _transport(token)
    transport._api_slots.in_flight = transport._api_slots.limit
    frames, _, _ = _serve(transport, _api(token))
    assert frames[-1] == {"type": "error", "detail": "rate_limited"}

    transport._api_slots.in_flight = 0
    slots = iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_PER_CONNECTION)
    frames, _, _ = _serve(transport, _api(token), slots=slots)
    assert frames[-1]["type"] == "response"
    assert slots.in_flight == 0
    assert transport._api_slots.in_flight == 0


def test_api_mode_switched_off_serves_the_console_only():
    token = _fake_token()
    app = EchoApp()
    transport = _transport(token, app, api_enabled=False)
    assert transport.capabilities == ["console"]
    frames, _, _ = _serve(transport, _api(token))
    assert frames[0]["capabilities"] == ["console"]
    assert frames[1] == {"type": "error", "detail": "forbidden_path"}
    assert app.calls == []


def test_the_key_is_never_logged(caplog):
    token = _fake_token()
    with caplog.at_level("DEBUG"):
        _serve(_transport(token), _api(token, "GET", "/api/jobs/job-1"))
        _serve(_transport(token), _api(_fake_token()))
    assert token not in caplog.text
    assert "iroh api GET /api/jobs/{id} -> 200" in caplog.text


# ----------------- The console protocol is unchanged -----------------

def test_a_console_request_still_streams_with_capabilities_in_hello():
    token = _fake_token()

    class OneRowDb:
        def get_console_logs(self, agent, limit, after_id):
            if after_id:
                return []
            return [{"id": 7, "agent": "codex", "source": "stdout", "content": "hi",
                     "timestamp": "2026-01-01T00:00:00Z", "session_id": None}]

    transport = IrohTransport(db=OneRowDb(), token_provider=lambda: token, asgi_app=EchoApp())
    bi = FakeBi({"token": token, "agent": "all", "backlog": 10})
    conn = FakeConn()

    async def run():
        task = asyncio.ensure_future(
            transport._handle_stream(conn, bi, iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_PER_CONNECTION))
        )
        for _ in range(100):
            if b'"type": "log"' in bi._send.buffer:
                break
            await asyncio.sleep(0.01)
        transport._closing = True
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(run())
    frames = bi._send.frames()
    assert frames[0]["type"] == "hello"
    assert frames[0]["app"] == "AgnView"
    assert frames[0]["protocol"] == 1
    assert frames[0]["capabilities"] == ["console", "api"]
    assert frames[1] == {"type": "log", "id": 7, "agent": "codex", "source": "stdout",
                         "content": "hi", "timestamp": "2026-01-01T00:00:00Z", "session_id": None}
    # A console stream still closes its connection when it ends.
    assert conn.closed


def test_a_malformed_request_keeps_its_old_detail():
    frames, conn, _ = _serve(_transport(_fake_token()), b"not json\n")
    assert frames == [{"type": "error", "detail": "malformed request"}]
    assert conn.closed


def test_the_hub_wires_its_own_app_into_the_transport(tmp_path):
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=_fake_token())
    assert app.state.iroh.capabilities == ["console", "api", "uploads"]
    assert app.state.iroh.status()["capabilities"] == ["console", "api", "uploads"]


def test_the_api_switch_turns_the_mode_off(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNVIEW_IROH_API", "0")
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=_fake_token())
    assert app.state.iroh.capabilities == ["console"]


def test_the_config_flag_turns_the_mode_off(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("iroh_api_enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("AGNVIEW_CONFIG", str(config_path))
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=_fake_token())
    assert app.state.config.iroh_api_enabled is False
    assert app.state.iroh.capabilities == ["console"]


def test_the_published_spec_lists_the_same_allowlist():
    from pathlib import Path

    spec_path = Path(__file__).resolve().parent.parent / "docs" / "mobile-api-spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))["x-agnview-iroh"]
    documented = [entry.split("?")[0] for entry in spec["allowlist"]]
    assert documented == [f"{method} {template}" for method, template, _ in iroh_api.API_ALLOWLIST]
    assert spec["limits"]["response_body_bytes"] == iroh_api.MAX_RESPONSE_BYTES
    assert spec["limits"]["request_bytes"] == transport_module.MAX_REQUEST_BYTES
    assert spec["limits"]["api_calls_per_connection"] == iroh_api.MAX_API_CALLS_PER_CONNECTION
    assert spec["limits"]["api_calls_per_hub"] == iroh_api.MAX_API_CALLS_TOTAL
    assert spec["limits"]["streams_per_connection"] == transport_module.MAX_STREAMS_PER_CONNECTION


def test_a_non_boolean_api_switch_is_refused(tmp_path, monkeypatch):
    from agent_relay.core.config import load_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("iroh_api_enabled: sometimes\n", encoding="utf-8")
    config = load_config(config_path)
    assert not config.is_valid
    assert any("iroh_api_enabled" in message for message in config.errors)
