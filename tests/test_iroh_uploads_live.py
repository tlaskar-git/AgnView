"""The upload protocol over real iroh endpoints on the loopback interface.

Both endpoints run in this process, bind 127.0.0.1 on a port the OS picks and
use no relay and no discovery, so nothing leaves the machine. The hub side is
the transport's real connection handler in front of the real FastAPI app.
"""

import asyncio
import hashlib
import json
import secrets

import pytest

from agent_relay.api.app import create_app
from agent_relay.core import iroh_api, uploads
from agent_relay.core.iroh_transport import IROH_RATE_LIMIT_KEY, IrohTransport, iroh_available
from agent_relay.core.pairing import reset_auth_rate_limit
from test_iroh_api_live import STEP_TIMEOUT_SECONDS, _read_frames, _request, _with_hub

pytestmark = pytest.mark.skipif(not iroh_available(), reason="no iroh wheel for this platform")

MIB = 1024 * 1024


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "free_bytes", lambda path: 500 * 1024**3)
    reset_auth_rate_limit(IROH_RATE_LIMIT_KEY)
    token = secrets.token_urlsafe(32)
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)
    transport = IrohTransport(
        db=app.state.db,
        token_provider=lambda: app.state.auth_token,
        asgi_app=app,
        uploads=app.state.uploads,
    )
    yield app, transport, token
    reset_auth_rate_limit(IROH_RATE_LIMIT_KEY)


async def _api(conn, token, method, path, body=None):
    return await _request(conn, {"token": token, "op": "api", "method": method, "path": path, "body": body})


async def _chunk(conn, token, upload_id, offset, data, declared=None, finish=True):
    stream = await conn.open_bi()
    send, recv = stream.send(), stream.recv()
    line = {"token": token, "op": "upload_chunk", "upload_id": upload_id, "offset": offset,
            "length": len(data) if declared is None else declared}
    await send.write_all((json.dumps(line) + "\n").encode("utf-8") + data)
    if finish:
        await send.finish()
    return await asyncio.wait_for(_read_frames(recv), timeout=STEP_TIMEOUT_SECONDS)


def test_a_multi_chunk_upload_over_real_iroh(hub):
    app, transport, token = hub
    data = secrets.token_bytes(2 * MIB + 4321)

    async def steps(conn):
        created = await _api(conn, token, "POST", "/api/uploads", {"name": "clip.mov", "size": len(data)})
        upload_id = created[1]["body"]["upload_id"]
        replies = []
        for offset in range(0, len(data), MIB):
            replies.append(await _chunk(conn, token, upload_id, offset, data[offset:offset + MIB]))
        status = await _api(conn, token, "GET", f"/api/uploads/{upload_id}")
        done = await _api(conn, token, "POST", f"/api/uploads/{upload_id}/finish",
                          {"sha256": hashlib.sha256(data).hexdigest()})
        return created, replies, status, done

    created, replies, status, done = asyncio.run(_with_hub(transport, steps))

    assert created[1]["status"] == 201 and created[1]["body"]["chunk_size"] == MIB
    assert [r[0]["type"] for r in replies] == ["hello"] * 3
    assert [r[0]["capabilities"] for r in replies] == [["console", "api", "uploads"]] * 3
    assert [r[1]["status"] for r in replies] == [200, 200, 200]
    assert [r[1]["body"]["received"] for r in replies] == [MIB, 2 * MIB, len(data)]
    assert status[1]["body"]["state"] == "receiving"
    assert done[1]["status"] == 200
    assert open(done[1]["body"]["path"], "rb").read() == data


def test_a_resume_after_a_dropped_connection(hub):
    app, transport, token = hub
    data = secrets.token_bytes(MIB + 100)
    state = {}

    async def first(conn):
        created = await _api(conn, token, "POST", "/api/uploads", {"name": "a.bin", "size": len(data)})
        state["id"] = created[1]["body"]["upload_id"]
        return await _chunk(conn, token, state["id"], 0, data[:MIB])

    async def second(conn):
        status = await _api(conn, token, "GET", f"/api/uploads/{state['id']}")
        received = status[1]["body"]["received"]
        await _chunk(conn, token, state["id"], received, data[received:])
        return await _api(conn, token, "POST", f"/api/uploads/{state['id']}/finish")

    asyncio.run(_with_hub(transport, first))
    done = asyncio.run(_with_hub(transport, second))
    assert open(done[1]["body"]["path"], "rb").read() == data


def test_a_short_body_and_a_long_body_are_refused_and_store_nothing(hub):
    app, transport, token = hub

    async def steps(conn):
        created = await _api(conn, token, "POST", "/api/uploads", {"name": "a.bin", "size": 20})
        upload_id = created[1]["body"]["upload_id"]
        short = await _chunk(conn, token, upload_id, 0, b"only5", declared=10)
        long = await _chunk(conn, token, upload_id, 0, b"0123456789", declared=5)
        huge = await _chunk(conn, token, upload_id, 0, b"x" * (MIB + 1), declared=MIB + 1)
        good = await _chunk(conn, token, upload_id, 0, b"0123456789")
        return short, long, huge, good

    short, long, huge, good = asyncio.run(_with_hub(transport, steps))
    assert short[1] == {"type": "error", "detail": "bad_request"}
    assert long[1] == {"type": "error", "detail": "bad_request"}
    assert huge[1] == {"type": "error", "detail": "too_large"}
    assert good[1]["status"] == 200 and good[1]["body"]["received"] == 10


def test_a_peer_that_never_finishes_is_timed_out_and_nothing_is_stored(hub, monkeypatch):
    app, transport, token = hub
    monkeypatch.setattr(iroh_api, "UPLOAD_BODY_TIMEOUT_SECONDS", 0.5)

    async def steps(conn):
        created = await _api(conn, token, "POST", "/api/uploads", {"name": "a.bin", "size": 8})
        upload_id = created[1]["body"]["upload_id"]
        stalled = await _chunk(conn, token, upload_id, 0, b"abcd", declared=8, finish=False)
        status = await _api(conn, token, "GET", f"/api/uploads/{upload_id}")
        return stalled, status

    stalled, status = asyncio.run(_with_hub(transport, steps))
    assert stalled[-1] == {"type": "error", "detail": "timeout"}
    assert status[1]["body"]["received"] == 0


def test_a_wrong_key_is_refused_over_real_iroh(hub):
    app, transport, token = hub

    async def steps(conn):
        created = await _api(conn, token, "POST", "/api/uploads", {"name": "a.bin", "size": 4})
        upload_id = created[1]["body"]["upload_id"]
        wrong = await _chunk(conn, secrets.token_urlsafe(32), upload_id, 0, b"abcd")
        return upload_id, wrong

    upload_id, wrong = asyncio.run(_with_hub(transport, steps))
    assert wrong == [{"type": "error", "detail": "unauthorised"}]
    assert app.state.uploads.status(upload_id)["received"] == 0
