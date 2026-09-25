"""Security review findings for the mobile API over iroh.

Every test here failed on the code as first submitted and passes with the fixes:
dispatch from iroh, the pairing routes and CORS, the iroh lockout, size limits
and the allowlist's trailing-newline hole. Keys and paths are generated per run.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core import iroh_api
from agent_relay.core.iroh_api import ApiError, forward_to_app, match_allowlist, parse_api_request
from agent_relay.core.iroh_transport import IrohTransport
from agent_relay.core.pairing import check_auth_rate_limit, reset_auth_rate_limit
from test_iroh_api import FakeBi, FakeConn, _api, _fake_token, _serve

PORT = 8765
LOOPBACK = ("127.0.0.1", 50000)


def _app(tmp_path, token=None):
    return create_app(db_path=str(tmp_path / "hub.db"), auth_token=token, port=PORT)


def _local_client(app, host="127.0.0.1", client=LOOPBACK):
    return TestClient(app, base_url=f"http://{host}:{PORT}", client=client)


def _record_dispatch(app):
    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)

    app.state.engine.runner.dispatch = fake_dispatch
    return calls


def _iroh_dispatch(app, token, body):
    async def run():
        request = parse_api_request(
            {"op": "api", "method": "POST", "path": "/api/console/dispatch", "body": body}
        )
        frame = await forward_to_app(app, request, token=token)
        # Let a dispatch task the route created run before the loop closes.
        await asyncio.sleep(0.05)
        return frame

    return asyncio.run(run())


# ----------------- Finding 1: dispatch over iroh -----------------

@pytest.mark.parametrize("agent", ["$whoami", "rm -rf x", "echo hello", "sh", "Claude Code"])
def test_iroh_dispatch_refuses_an_unknown_agent(tmp_path, agent):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    frame = _iroh_dispatch(app, token, {"agent": agent, "prompt": "hi"})
    assert frame["status"] == 422
    assert frame["body"]["detail"] == "unknown_agent"
    assert calls == []


@pytest.mark.parametrize("agent", ["claude_code", "claude", "codex", "antigravity", "deepseek", "all"])
def test_iroh_dispatch_allows_a_named_agent(tmp_path, agent):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    frame = _iroh_dispatch(app, token, {"agent": agent, "prompt": "hi"})
    assert frame["status"] == 200
    assert len(calls) == 1


def test_iroh_dispatch_allows_an_enabled_adapter_only(tmp_path):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    adapters = app.state.engine.runner.adapter_manager.adapters
    adapters["helper"] = SimpleNamespace(id="helper", enabled=True)
    adapters["parked"] = SimpleNamespace(id="parked", enabled=False)
    assert _iroh_dispatch(app, token, {"agent": "helper", "prompt": "hi"})["status"] == 200
    frame = _iroh_dispatch(app, token, {"agent": "parked", "prompt": "hi"})
    assert frame["status"] == 422 and frame["body"]["detail"] == "unknown_agent"
    assert len(calls) == 1


def test_lan_dispatch_is_unchanged(tmp_path):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    client = _local_client(app, client=("192.0.2.7", 40000))
    response = client.post(
        "/api/console/dispatch",
        json={"agent": "echo hello", "prompt": "hi", "working_directory": "does-not-exist"},
        headers={"X-AgnView-Token": token},
    )
    assert response.status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("bad", [
    "\\\\host\\share",
    "\\\\host\\share\\dir",
    "\\\\?\\C:\\dir",
    "\\\\.\\pipe\\name",
    "//host/share",
    "dir\x00name",
    "no-such-directory-for-agnview-tests",
    "",
])
def test_iroh_dispatch_refuses_a_bad_working_directory(tmp_path, bad):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    body = {"agent": "codex", "prompt": "hi", "working_directory": bad}
    frame = _iroh_dispatch(app, token, body)
    assert frame["status"] == 422
    assert frame["body"]["detail"] == "invalid_working_directory"
    assert calls == []


def test_iroh_dispatch_refuses_a_file_as_working_directory(tmp_path):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    target = tmp_path / "a-file.txt"
    target.write_text("x")
    frame = _iroh_dispatch(app, token, {"agent": "codex", "prompt": "hi", "working_directory": str(target)})
    assert frame["status"] == 422 and calls == []


def test_iroh_dispatch_allows_any_existing_directory_by_default(tmp_path):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    frame = _iroh_dispatch(app, token, {"agent": "codex", "prompt": "hi", "working_directory": str(tmp_path)})
    assert frame["status"] == 200
    assert calls[0]["cwd"] == str(tmp_path.resolve())


def test_iroh_dispatch_roots_confine_the_directory(tmp_path):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    root = tmp_path / "work"
    inside = root / "project"
    outside = tmp_path / "elsewhere"
    inside.mkdir(parents=True)
    outside.mkdir()
    app.state.config.iroh_dispatch_roots = [str(root)]

    ok = _iroh_dispatch(app, token, {"agent": "codex", "prompt": "hi", "working_directory": str(inside)})
    assert ok["status"] == 200
    no = _iroh_dispatch(app, token, {"agent": "codex", "prompt": "hi", "working_directory": str(outside)})
    assert no["status"] == 422 and no["body"]["detail"] == "invalid_working_directory"
    # A path that climbs out of the root is judged after it is resolved.
    sneaky = str(root / ".." / "elsewhere")
    no = _iroh_dispatch(app, token, {"agent": "codex", "prompt": "hi", "working_directory": sneaky})
    assert no["status"] == 422
    assert len(calls) == 1


def test_iroh_dispatch_with_roots_and_no_directory_uses_the_first_root(tmp_path):
    token = _fake_token()
    app = _app(tmp_path, token)
    calls = _record_dispatch(app)
    root = tmp_path / "work"
    root.mkdir()
    app.state.config.iroh_dispatch_roots = [str(root)]
    assert _iroh_dispatch(app, token, {"agent": "codex", "prompt": "hi"})["status"] == 200
    assert calls[0]["cwd"] == str(root.resolve())


def test_the_dispatch_roots_setting_is_read_and_validated(tmp_path):
    from agent_relay.core.config import load_config

    good = tmp_path / "good.yaml"
    good.write_text("iroh_dispatch_roots:\n  - /srv/work\n")
    assert load_config(good).iroh_dispatch_roots == ["/srv/work"]
    assert load_config(tmp_path / "missing.yaml").iroh_dispatch_roots == []
    bad = tmp_path / "bad.yaml"
    bad.write_text("iroh_dispatch_roots: nope\n")
    assert not load_config(bad).is_valid


# ----------------- Finding 2: pairing routes and CORS -----------------

def test_a_foreign_origin_gets_neither_cors_nor_the_key(tmp_path):
    token = _fake_token()
    client = _local_client(_app(tmp_path, token))
    response = client.get("/api/mobile/pairing", headers={"Origin": "https://untrusted.example"})
    assert "access-control-allow-origin" not in response.headers
    assert response.status_code == 403
    assert token not in response.text


def test_no_route_sends_a_wildcard_cors_header(tmp_path):
    token = _fake_token()
    client = _local_client(_app(tmp_path, token))
    response = client.options(
        "/api/mobile/status",
        headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers
    response = client.get("/api/mobile/status", headers={"Origin": "https://untrusted.example", "X-AgnView-Token": token})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("token", [None, "set"])
def test_pairing_routes_refuse_a_client_that_is_not_loopback(tmp_path, token):
    token = _fake_token() if token else None
    client = _local_client(_app(tmp_path, token), client=("192.0.2.9", 40000))
    headers = {"X-AgnView-Token": token} if token else {}
    assert client.get("/api/mobile/pairing", headers=headers).status_code == 403
    assert client.post("/api/mobile/pairing/regenerate", headers=headers).status_code == 403
    # No token at all from a LAN client: refused, not answered with the key.
    if token:
        response = client.get("/api/mobile/pairing")
        assert response.status_code == 403 and token not in response.text


def test_pairing_routes_refuse_a_client_that_is_not_loopback_even_via_iroh(tmp_path):
    token = _fake_token()
    app = _app(tmp_path, token)
    request = SimpleNamespace(method="GET", path="/api/mobile/pairing", query="", template="", body=None)
    frame = asyncio.run(forward_to_app(app, request, token=token))
    assert frame["status"] == 403


@pytest.mark.parametrize("headers,expected", [
    ({}, 200),
    ({"Origin": f"http://127.0.0.1:{PORT}"}, 200),
    ({"Origin": f"http://localhost:{PORT}"}, 403),
    ({"Origin": "https://untrusted.example"}, 403),
    ({"Origin": "null"}, 403),
    ({"Sec-Fetch-Site": "same-origin"}, 200),
    ({"Sec-Fetch-Site": "none"}, 200),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Sec-Fetch-Site": "same-site"}, 403),
    ({"Host": "rebind.example:8765"}, 403),
    ({"Host": "127.0.0.1"}, 403),
    ({"Host": "127.0.0.1:9999"}, 403),
    ({"Host": f"localhost:{PORT}"}, 200),
    ({"Host": f"[::1]:{PORT}"}, 200),
])
def test_pairing_route_request_checks(tmp_path, headers, expected):
    token = _fake_token()
    client = _local_client(_app(tmp_path, token))
    response = client.get("/api/mobile/pairing", headers=headers)
    assert response.status_code == expected
    if expected == 403:
        assert token not in response.text


def test_regenerate_needs_the_same_checks(tmp_path):
    client = _local_client(_app(tmp_path, _fake_token()))
    bad = client.post("/api/mobile/pairing/regenerate", headers={"Origin": "https://untrusted.example"})
    assert bad.status_code == 403
    good = client.post("/api/mobile/pairing/regenerate", headers={"Origin": f"http://127.0.0.1:{PORT}"})
    assert good.status_code == 200


def test_the_dashboard_and_desktop_paths_still_work(tmp_path):
    """The dashboard fetches same-origin with a same-origin fetch header."""
    token = _fake_token()
    client = _local_client(_app(tmp_path, token))
    headers = {"Sec-Fetch-Site": "same-origin", "Referer": f"http://127.0.0.1:{PORT}/"}
    assert client.get("/api/mobile/pairing", headers=headers).status_code == 200
    assert client.get("/api/mobile/status", headers=headers).status_code == 200
    assert client.get("/").status_code == 200


# ----------------- Finding 3: iroh lockout -----------------

class PeerConn(FakeConn):
    def __init__(self, peer):
        super().__init__()
        self._peer = peer

    def remote_id(self):
        return self._peer


def _serve_from(transport, peer, request):
    bi = FakeBi(request)
    conn = PeerConn(peer)
    slots = iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_PER_CONNECTION)
    asyncio.run(transport._handle_stream(conn, bi, slots))
    return bi._send.frames()


@pytest.fixture
def clean_peer_limits():
    peers = [f"peer-{i}" for i in range(80)] + ["peer-a", "peer-b"]
    keys = [f"iroh:{p}" for p in peers] + ["iroh", "iroh:unknown", "127.0.0.1"]
    for key in keys:
        reset_auth_rate_limit(key)
    yield
    for key in keys:
        reset_auth_rate_limit(key)


def _transport(token):
    return IrohTransport(token_provider=lambda: token, asgi_app=_EchoApp(), api_enabled=True)


class _EchoApp:
    async def __call__(self, scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})


def _is_limited(frames):
    return frames == [{"type": "error", "detail": "rate_limited"}]


def test_one_peers_wrong_keys_do_not_block_another_peers_valid_key(clean_peer_limits):
    token = _fake_token()
    transport = _transport(token)
    for _ in range(10):
        _serve_from(transport, "peer-a", _api(_fake_token()))
    assert _is_limited(_serve_from(transport, "peer-a", _api(_fake_token())))
    frames = _serve_from(transport, "peer-b", _api(token))
    assert frames[0]["type"] == "hello" and frames[-1]["type"] == "response"


def test_a_valid_key_is_never_blocked_even_for_the_throttled_peer(clean_peer_limits):
    token = _fake_token()
    transport = _transport(token)
    for _ in range(12):
        _serve_from(transport, "peer-a", _api(_fake_token()))
    frames = _serve_from(transport, "peer-a", _api(token))
    assert frames[-1]["type"] == "response"
    # And it clears that peer's record, so wrong keys count from zero again.
    assert _serve_from(transport, "peer-a", _api(_fake_token())) == [{"type": "error", "detail": "unauthorised"}]


def test_the_same_peer_is_throttled_for_invalid_attempts_only(clean_peer_limits):
    token = _fake_token()
    transport = _transport(token)
    for _ in range(10):
        assert _serve_from(transport, "peer-a", {"token": _fake_token()}) == [{"type": "error", "detail": "unauthorised"}]
    assert _is_limited(_serve_from(transport, "peer-a", {"token": _fake_token()}))


def test_many_peers_hit_a_global_cap_that_never_blocks_a_valid_key(clean_peer_limits):
    token = _fake_token()
    transport = _transport(token)
    for i in range(60):
        frames = _serve_from(transport, f"peer-{i}", _api(_fake_token()))
        assert frames == [{"type": "error", "detail": "unauthorised"}]
    assert _is_limited(_serve_from(transport, "peer-70", _api(_fake_token())))
    frames = _serve_from(transport, "peer-71", _api(token))
    assert frames[-1]["type"] == "response"


def test_iroh_failures_never_touch_the_lan_or_the_shared_iroh_key(clean_peer_limits):
    token = _fake_token()
    transport = _transport(token)
    for _ in range(10):
        _serve_from(transport, "peer-a", _api(_fake_token()))
    assert check_auth_rate_limit("127.0.0.1")
    assert check_auth_rate_limit("iroh")
    assert not check_auth_rate_limit("iroh:peer-a")


def test_a_connection_with_no_peer_id_still_counts_under_one_key(clean_peer_limits):
    token = _fake_token()
    transport = _transport(token)
    for _ in range(10):
        _serve(transport, _api(_fake_token()))
    assert not check_auth_rate_limit("iroh:unknown")


# ----------------- Finding 4: limits -----------------

@pytest.mark.parametrize("limit,expected", [(1, 200), (1000, 200), (1001, 422), (0, 422), (100000, 422)])
def test_console_logs_limit_is_bounded(tmp_path, limit, expected):
    token = _fake_token()
    client = _local_client(_app(tmp_path, token))
    response = client.get(f"/api/console/logs?limit={limit}", headers={"X-AgnView-Token": token})
    assert response.status_code == expected


@pytest.mark.parametrize("query", [
    "limit=1001", "limit=999999999999", "limit=abc", "limit=", "limit=0", "limit=-1",
    "limit=1.5", "after_id=abc", "after_id=", "after_id=-3", "after_id=99999999999999999999",
])
def test_the_allowlist_refuses_a_bad_size_or_id(query):
    with pytest.raises(ApiError) as caught:
        match_allowlist("GET", "/api/console/logs?" + query)
    assert caught.value.code == "forbidden_path"


@pytest.mark.parametrize("query", ["limit=1", "limit=1000", "limit=50&after_id=0", "agent=all&limit=250&after_id=12"])
def test_the_allowlist_accepts_a_good_size_and_id(query):
    assert match_allowlist("GET", "/api/console/logs?" + query)[2] == "/api/console/logs"


def test_the_console_stream_backlog_is_capped(clean_peer_limits):
    token = _fake_token()
    transport = _transport(token)
    seen = []

    async def read_console(agent, limit, after_id):
        seen.append(limit)
        transport._closing = True
        return []

    transport._read_console = read_console
    _serve_from(transport, "peer-a", {"token": token, "backlog": 10**9})
    assert seen == [iroh_api.MAX_PAGE_SIZE]


# ----------------- Finding 5: trailing newline -----------------

@pytest.mark.parametrize("suffix", ["\n", "\r\n", "\r", " "])
@pytest.mark.parametrize("target", [
    "/api/mobile/status", "/api/jobs/job-1", "/api/console/logs?limit=5",
    "/api/console/logs?agent=all",
])
def test_a_trailing_control_character_is_refused(target, suffix):
    with pytest.raises(ApiError) as caught:
        match_allowlist("GET", target + suffix)
    assert caught.value.code == "forbidden_path"
