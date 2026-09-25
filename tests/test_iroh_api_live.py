"""The iroh protocol end to end, over real iroh endpoints on the loopback interface.

Both endpoints run in this process, bind 127.0.0.1 on a port the OS picks and
use no relay and no discovery, so the test never leaves the machine. The hub
side is the transport's real connection handler in front of the real FastAPI
app. Only the agent launcher is stubbed, so no agent CLI ever runs.
"""

import asyncio
import json
import secrets

import pytest

from agent_relay.api.app import create_app
from agent_relay.core.iroh_transport import (
    IROH_ALPN,
    IROH_RATE_LIMIT_KEY,
    IrohTransport,
    iroh_available,
)
from agent_relay.core.pairing import reset_auth_rate_limit

pytestmark = pytest.mark.skipif(not iroh_available(), reason="no iroh wheel for this platform")

STEP_TIMEOUT_SECONDS = 20.0


def _loopback_options(**extra):
    import iroh

    return iroh.EndpointOptions(
        preset=iroh.preset_minimal(),
        bind_addr="127.0.0.1:0",
        relay_mode=iroh.RelayMode.disabled(),
        **extra,
    )


async def _read_frames(recv):
    buffer = b""
    while True:
        chunk = await recv.read(64 * 1024)
        if not chunk:
            break
        buffer += chunk
    return [json.loads(line) for line in buffer.decode("utf-8").splitlines() if line.strip()]


async def _request(conn, request):
    stream = await conn.open_bi()
    send, recv = stream.send(), stream.recv()
    await send.write_all((json.dumps(request) + "\n").encode("utf-8"))
    await send.finish()
    return await asyncio.wait_for(_read_frames(recv), timeout=STEP_TIMEOUT_SECONDS)


async def _with_hub(transport, client_steps):
    """Serve one client connection through the transport's real handler."""
    import iroh

    server = await iroh.Endpoint.bind(_loopback_options(alpns=[IROH_ALPN]))
    client = await iroh.Endpoint.bind(_loopback_options())
    handler = None
    try:
        async def accept_one():
            incoming = await server.accept_next()
            await transport._handle_incoming(incoming)

        handler = asyncio.ensure_future(accept_one())
        conn = await asyncio.wait_for(client.connect(server.addr(), IROH_ALPN), timeout=STEP_TIMEOUT_SECONDS)
        try:
            return await client_steps(conn)
        finally:
            conn.close(0, b"done")
    finally:
        if handler is not None:
            handler.cancel()
            try:
                await handler
            except BaseException:
                pass
        await client.close()
        await server.close()


@pytest.fixture
def hub(tmp_path):
    reset_auth_rate_limit(IROH_RATE_LIMIT_KEY)
    token = secrets.token_urlsafe(32)
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)

    dispatched = []

    async def fake_dispatch(**kwargs):
        dispatched.append(kwargs)

    app.state.engine.runner.dispatch = fake_dispatch
    transport = IrohTransport(
        db=app.state.db,
        token_provider=lambda: app.state.auth_token,
        asgi_app=app,
    )
    yield app, transport, token, dispatched
    reset_auth_rate_limit(IROH_RATE_LIMIT_KEY)


def test_api_round_trip_over_real_iroh(hub):
    app, transport, token, dispatched = hub

    async def steps(conn):
        status = await _request(conn, {
            "token": token, "op": "api", "method": "GET", "path": "/api/mobile/status", "body": None,
        })
        # A second request on the same connection: API streams leave it open.
        dispatch = await _request(conn, {
            "token": token, "op": "api", "method": "POST", "path": "/api/console/dispatch",
            "body": {"agent": "codex", "prompt": "say hello"},
        })
        forbidden = await _request(conn, {
            "token": token, "op": "api", "method": "GET", "path": "/api/mobile/pairing", "body": None,
        })
        return status, dispatch, forbidden

    status, dispatch, forbidden = asyncio.run(_with_hub(transport, steps))

    assert [f["type"] for f in status] == ["hello", "response"]
    assert status[0]["capabilities"] == ["console", "api"]
    assert status[1]["status"] == 200
    assert status[1]["body"]["status"] == "healthy"

    assert [f["type"] for f in dispatch] == ["hello", "response"]
    assert dispatch[1]["status"] == 200
    assert dispatch[1]["body"]["status"] == "dispatched"
    assert dispatch[1]["body"]["agent"] == "codex"
    assert len(dispatched) == 1
    assert dispatched[0]["agent"] == "codex"
    assert dispatched[0]["prompt"] == "say hello"

    assert forbidden == [forbidden[0], {"type": "error", "detail": "forbidden_path"}]
    assert forbidden[0]["type"] == "hello"


def test_a_wrong_key_is_unauthorised_over_real_iroh(hub):
    app, transport, token, dispatched = hub

    async def steps(conn):
        return await _request(conn, {
            "token": secrets.token_urlsafe(32), "op": "api", "method": "POST",
            "path": "/api/console/dispatch", "body": {"agent": "codex", "prompt": "x"},
        })

    frames = asyncio.run(_with_hub(transport, steps))
    assert frames == [{"type": "error", "detail": "unauthorised"}]
    assert dispatched == []


def test_the_console_still_streams_over_real_iroh(hub):
    app, transport, token, _ = hub
    app.state.db.add_console_log(agent="codex", source="stdout", content="first line")

    async def steps(conn):
        stream = await conn.open_bi()
        send, recv = stream.send(), stream.recv()
        await send.write_all((json.dumps({"token": token, "agent": "all", "backlog": 10}) + "\n").encode("utf-8"))
        await send.finish()
        buffer = b""
        while buffer.count(b"\n") < 2:
            chunk = await asyncio.wait_for(recv.read(64 * 1024), timeout=STEP_TIMEOUT_SECONDS)
            if not chunk:
                break
            buffer += chunk
        return [json.loads(line) for line in buffer.decode("utf-8").splitlines() if line.strip()]

    frames = asyncio.run(_with_hub(transport, steps))
    assert frames[0]["type"] == "hello"
    assert frames[0]["capabilities"] == ["console", "api"]
    assert frames[1]["type"] == "log"
    assert frames[1]["content"] == "first line"


def _load_reference_client():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "tools" / "iroh-client.py"
    spec = importlib.util.spec_from_file_location("agnview_iroh_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_reference_client_makes_an_api_call(hub, monkeypatch, capsys):
    import iroh

    app, transport, token, _ = hub
    client_module = _load_reference_client()
    assert client_module.IROH_ALPN == IROH_ALPN

    async def run():
        server = await iroh.Endpoint.bind(_loopback_options(alpns=[IROH_ALPN]))

        async def accept_one():
            incoming = await server.accept_next()
            await transport._handle_incoming(incoming)

        async def loopback_connect(ticket):
            endpoint = await iroh.Endpoint.bind(_loopback_options())
            conn = await endpoint.connect(server.addr(), IROH_ALPN)
            return endpoint, conn

        monkeypatch.setattr(client_module, "_connect_iroh", loopback_connect)
        handler = asyncio.ensure_future(accept_one())
        try:
            return await asyncio.wait_for(
                client_module.request_over_iroh("unused-ticket", token, "GET", "/api/jobs", None),
                timeout=STEP_TIMEOUT_SECONDS,
            )
        finally:
            handler.cancel()
            try:
                await handler
            except BaseException:
                pass
            await server.close()

    assert asyncio.run(run()) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == []
    assert "capabilities: console, api" in captured.err
    assert token not in captured.out + captured.err
