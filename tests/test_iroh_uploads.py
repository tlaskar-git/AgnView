"""Uploads over iroh: the upload_chunk framing, the API calls that go with it,
the allowlist entries and the switches.

The streams here are in memory, the same fakes the other iroh tests use, so no
network is involved. tests/test_iroh_uploads_live.py runs the same protocol over
real iroh endpoints on the loopback interface.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets

import pytest

from agent_relay.api.app import create_app
from agent_relay.core import iroh_api, uploads
from agent_relay.core.iroh_api import ApiError, match_allowlist, parse_upload_chunk_request
from agent_relay.core.iroh_transport import MAX_REQUEST_BYTES, IrohTransport
from agent_relay.core.pairing import reset_auth_rate_limit
from test_iroh_api import FakeBi, FakeConn, FakeRecv, _fake_token

MIB = 1024 * 1024
PEERS = ("peer-a", "peer-b", "unknown")


class PeerConn(FakeConn):
    def __init__(self, peer="peer-a"):
        super().__init__()
        self._peer = peer

    def remote_id(self):
        return self._peer


class StallingRecv(FakeRecv):
    """Delivers `first`, then never sends another byte."""

    def __init__(self, first: bytes):
        super().__init__(first)
        self._calls = 0

    async def read(self, size_limit):
        self._calls += 1
        if self._calls > 1:
            await asyncio.sleep(3600)
        return await super().read(size_limit)


class DroppingRecv(FakeRecv):
    """Delivers `first`, then the connection drops."""

    def __init__(self, first: bytes):
        super().__init__(first)
        self._calls = 0

    async def read(self, size_limit):
        self._calls += 1
        if self._calls > 1:
            raise ConnectionError("peer went away")
        return await super().read(size_limit)


@pytest.fixture(autouse=True)
def clean_limiter():
    keys = ["iroh"] + [f"iroh:{peer}" for peer in PEERS]
    for key in keys:
        reset_auth_rate_limit(key)
    yield
    for key in keys:
        reset_auth_rate_limit(key)


@pytest.fixture
def free(monkeypatch):
    monkeypatch.setattr(uploads, "free_bytes", lambda path: 500 * 1024**3)


@pytest.fixture
def hub(tmp_path, free):
    token = _fake_token()
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)
    transport = IrohTransport(
        token_provider=lambda: app.state.auth_token,
        asgi_app=app,
        uploads=app.state.uploads,
    )
    return app, transport, token


def _line(token, upload_id, offset, length, **extra):
    request = {"token": token, "op": "upload_chunk", "upload_id": upload_id, "offset": offset, "length": length}
    request.update(extra)
    return (json.dumps(request) + "\n").encode("utf-8")


def _serve(transport, raw=None, recv=None, conn=None, slots=None):
    bi = FakeBi(raw if raw is not None else b"")
    if recv is not None:
        bi._recv = recv
    slots = slots or iroh_api.ApiSlots(iroh_api.MAX_API_CALLS_PER_CONNECTION)
    conn = conn or PeerConn()
    asyncio.run(transport._handle_stream(conn, bi, slots))
    return bi._send.frames(), conn, bi


def _api(transport, token, method, path, body=None, peer="peer-a"):
    request = {"token": token, "op": "api", "method": method, "path": path, "body": body}
    frames, _, _ = _serve(transport, (json.dumps(request) + "\n").encode(), conn=PeerConn(peer))
    return frames


def _create(transport, token, size, name="photo.jpg", peer="peer-a"):
    frames = _api(transport, token, "POST", "/api/uploads", {"name": name, "size": size}, peer)
    assert frames[1]["status"] == 201, frames
    return frames[1]["body"]


def _chunk(transport, token, upload_id, offset, data, peer="peer-a", **line_extra):
    raw = _line(token, upload_id, offset, len(data), **line_extra) + data
    frames, _, _ = _serve(transport, raw, conn=PeerConn(peer))
    return frames


# ----------------- The framing -----------------

def test_a_whole_upload_over_the_api_ops_and_upload_chunk(hub):
    app, transport, token = hub
    data = secrets.token_bytes(2 * MIB + 123)
    info = _create(transport, token, len(data))
    upload_id = info["upload_id"]
    assert info["chunk_size"] == MIB

    for offset in range(0, len(data), MIB):
        frames = _chunk(transport, token, upload_id, offset, data[offset:offset + MIB])
        assert [f["type"] for f in frames] == ["hello", "response"]
        assert frames[0]["capabilities"] == ["console", "api", "uploads"]
        assert frames[1]["status"] == 200
        assert frames[1]["body"]["received"] == min(offset + MIB, len(data))

    status = _api(transport, token, "GET", f"/api/uploads/{upload_id}")[1]["body"]
    assert (status["received"], status["state"]) == (len(data), "receiving")
    done = _api(transport, token, "POST", f"/api/uploads/{upload_id}/finish",
                {"sha256": hashlib.sha256(data).hexdigest()})[1]
    assert done["status"] == 200
    assert open(done["body"]["path"], "rb").read() == data
    deleted = _api(transport, token, "DELETE", f"/api/uploads/{upload_id}")[1]
    assert (deleted["status"], deleted["body"]["status"]) == (200, "cancelled")


def test_the_stream_is_closed_and_the_connection_kept_after_a_chunk(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 4)["upload_id"]
    frames, conn, bi = _serve(transport, _line(token, upload_id, 0, 4) + b"abcd")
    assert bi._send.finished and not conn.closed


def test_a_chunk_that_arrives_in_pieces_is_put_together(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 10)["upload_id"]

    class Trickle(FakeRecv):
        async def read(self, size_limit):
            return await super().read(min(size_limit, 3))

    raw = _line(token, upload_id, 0, 10) + b"0123456789"
    frames, _, _ = _serve(transport, recv=Trickle(raw))
    assert frames[1]["status"] == 200 and frames[1]["body"]["received"] == 10


def test_a_short_body_stores_nothing(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 10)["upload_id"]
    frames, _, _ = _serve(transport, _line(token, upload_id, 0, 10) + b"only5")
    assert frames[1] == {"type": "error", "detail": "bad_request"}
    assert app.state.uploads.status(upload_id)["received"] == 0


def test_a_long_body_stores_nothing(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 10)["upload_id"]
    frames, _, _ = _serve(transport, _line(token, upload_id, 0, 5) + b"0123456789")
    assert frames[1] == {"type": "error", "detail": "bad_request"}
    assert app.state.uploads.status(upload_id)["received"] == 0


def test_one_byte_over_the_length_is_still_a_long_body(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 10)["upload_id"]
    frames, _, _ = _serve(transport, _line(token, upload_id, 0, 5) + b"012345")
    assert frames[1]["detail"] == "bad_request"
    assert app.state.uploads.status(upload_id)["received"] == 0


def test_a_length_over_one_mebibyte_is_too_large_and_the_body_is_not_read(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 3 * MIB)["upload_id"]
    body = b"x" * (MIB + 1)
    recv = FakeRecv(_line(token, upload_id, 0, MIB + 1) + body)
    frames, _, _ = _serve(transport, recv=recv)
    assert frames[1] == {"type": "error", "detail": "too_large"}
    assert app.state.uploads.status(upload_id)["received"] == 0
    assert len(recv._data) > MIB - MAX_REQUEST_BYTES - 1  # most of it was never taken off the stream


def test_a_body_bigger_than_a_mebibyte_under_a_valid_length_is_refused(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 3 * MIB)["upload_id"]
    frames, _, _ = _serve(transport, _line(token, upload_id, 0, MIB) + b"x" * (MIB + 10))
    assert frames[1]["detail"] == "bad_request"
    assert app.state.uploads.status(upload_id)["received"] == 0


def test_exactly_one_mebibyte_is_accepted(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, MIB)["upload_id"]
    frames = _chunk(transport, token, upload_id, 0, b"x" * MIB)
    assert frames[1]["status"] == 200


def test_a_peer_that_stalls_mid_body_times_out_and_stores_nothing(hub, monkeypatch):
    app, transport, token = hub
    monkeypatch.setattr(iroh_api, "UPLOAD_BODY_TIMEOUT_SECONDS", 0.05)
    upload_id = _create(transport, token, 100)["upload_id"]
    first = _line(token, upload_id, 0, 100) + b"x" * 40
    frames, _, _ = _serve(transport, recv=StallingRecv(first))
    assert frames[1] == {"type": "error", "detail": "timeout"}
    assert app.state.uploads.status(upload_id)["received"] == 0
    # The slot came back: the next chunk goes through.
    assert _chunk(transport, token, upload_id, 0, b"x" * 100)[1]["status"] == 200


def test_a_peer_that_never_finishes_its_send_side_times_out(hub, monkeypatch):
    app, transport, token = hub
    monkeypatch.setattr(iroh_api, "UPLOAD_BODY_TIMEOUT_SECONDS", 0.05)
    upload_id = _create(transport, token, 4)["upload_id"]

    class NeverFinishes(FakeRecv):
        async def read(self, size_limit):
            if not self._data:
                await asyncio.sleep(3600)
            return await super().read(size_limit)

    frames, _, _ = _serve(transport, recv=NeverFinishes(_line(token, upload_id, 0, 4) + b"abcd"))
    assert frames[1] == {"type": "error", "detail": "timeout"}
    assert app.state.uploads.status(upload_id)["received"] == 0


def test_a_line_that_never_ends_times_out_like_any_request(hub, monkeypatch):
    app, transport, token = hub
    from agent_relay.core import iroh_transport

    monkeypatch.setattr(iroh_transport, "REQUEST_READ_TIMEOUT_SECONDS", 0.05)
    frames, _, _ = _serve(transport, recv=FakeRecv(b"", hang=True))
    assert frames == [{"type": "error", "detail": "timeout"}]


def test_a_peer_that_drops_mid_chunk_changes_nothing(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 100)["upload_id"]
    first = _line(token, upload_id, 0, 100) + b"x" * 40
    frames, conn, _ = _serve(transport, recv=DroppingRecv(first))
    assert app.state.uploads.status(upload_id)["received"] == 0
    assert app.state.uploads.active_count() == 1
    assert _chunk(transport, token, upload_id, 0, b"x" * 100)[1]["status"] == 200


def test_a_request_line_over_the_cap_is_too_large(hub):
    app, transport, token = hub
    raw = _line(token, "a" * 32, 0, 1, pad="x" * MAX_REQUEST_BYTES) + b"x"
    frames, _, _ = _serve(transport, raw)
    assert frames == [{"type": "error", "detail": "too_large"}]


# ----------------- Chunk rules are the manager's, on both transports -----------------

def test_offset_errors_come_back_as_a_409_response_with_where_the_upload_stands(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 10)["upload_id"]
    _chunk(transport, token, upload_id, 0, b"0123")
    gap = _chunk(transport, token, upload_id, 6, b"6789")[1]
    assert (gap["type"], gap["status"], gap["body"]["detail"], gap["body"]["received"]) == (
        "response", 409, "offset_mismatch", 4)
    overlap = _chunk(transport, token, upload_id, 2, b"2345")[1]
    assert overlap["status"] == 409
    over = _chunk(transport, token, upload_id, 4, b"456789A")[1]
    assert (over["status"], over["body"]["detail"]) == (413, "beyond_declared_size")
    assert app.state.uploads.status(upload_id)["received"] == 4


def test_a_resent_last_chunk_is_stored_once(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 8)["upload_id"]
    assert _chunk(transport, token, upload_id, 0, b"0123")[1]["status"] == 200
    assert _chunk(transport, token, upload_id, 0, b"0123")[1]["body"]["received"] == 4
    assert _chunk(transport, token, upload_id, 4, b"4567")[1]["body"]["received"] == 8


def test_an_unknown_upload_is_a_404_response(hub):
    app, transport, token = hub
    frame = _chunk(transport, token, "0" * 32, 0, b"x")[1]
    assert (frame["status"], frame["body"]["detail"]) == (404, "not_found")


def test_an_upload_folder_holds_only_what_was_sent(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 3, name="..\\..\\evil.sh")["upload_id"]
    _chunk(transport, token, upload_id, 0, b"abc")
    done = _api(transport, token, "POST", f"/api/uploads/{upload_id}/finish")[1]["body"]
    assert os.path.basename(done["path"]) == "evil.sh"
    assert os.path.dirname(os.path.dirname(done["path"])) == os.path.realpath(app.state.uploads.root)


# ----------------- Bad request lines -----------------

@pytest.mark.parametrize("change", [
    {"upload_id": "A" * 32},
    {"upload_id": "a" * 31},
    {"upload_id": "../" + "a" * 29},
    {"upload_id": 5},
    {"upload_id": None},
    {"offset": -1},
    {"offset": "0"},
    {"offset": 1.5},
    {"offset": True},
    {"offset": 2**60},
    {"length": 0},
    {"length": -3},
    {"length": "4"},
    {"length": True},
    {"length": None},
    {"extra": "field"},
])
def test_a_malformed_line_is_refused_and_nothing_is_stored(hub, change):
    app, transport, token = hub
    upload_id = _create(transport, token, 4)["upload_id"]
    request = {"token": token, "op": "upload_chunk", "upload_id": upload_id, "offset": 0, "length": 4}
    request.update(change)
    frames, _, _ = _serve(transport, (json.dumps(request) + "\n").encode() + b"abcd")
    assert frames[-1]["type"] == "error"
    assert frames[-1]["detail"] in ("bad_request", "too_large")
    assert app.state.uploads.status(upload_id)["received"] == 0


@pytest.mark.parametrize("missing", ["upload_id", "offset", "length"])
def test_a_line_missing_a_field_is_refused(hub, missing):
    app, transport, token = hub
    upload_id = _create(transport, token, 4)["upload_id"]
    request = {"token": token, "op": "upload_chunk", "upload_id": upload_id, "offset": 0, "length": 4}
    del request[missing]
    frames, _, _ = _serve(transport, (json.dumps(request) + "\n").encode() + b"abcd")
    assert frames[-1] == {"type": "error", "detail": "bad_request"}


def test_the_parser_takes_only_the_documented_keys():
    assert parse_upload_chunk_request(
        {"token": "t", "op": "upload_chunk", "upload_id": "a" * 32, "offset": 5, "length": 9}
    ) == ("a" * 32, 5, 9)
    with pytest.raises(ApiError):
        parse_upload_chunk_request({"op": "upload_chunk", "upload_id": "a" * 32, "offset": 0, "length": 1, "x": 1})


def test_a_console_request_that_mentions_the_op_is_not_an_upload(hub):
    app, transport, token = hub
    raw = (json.dumps({"token": token, "agent": "upload_chunk", "backlog": 1}) + "\n").encode()
    assert asyncio.run(IrohTransport._read_request(FakeRecv(raw))) == (raw, None)
    api = (json.dumps({"op": "api", "method": "GET", "path": "/api/jobs"}) + "\n").encode()
    assert asyncio.run(IrohTransport._read_request(FakeRecv(api))) == (api, None)


# ----------------- Auth -----------------

def test_a_wrong_or_missing_key_is_refused_before_the_body_is_read(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 3 * MIB)["upload_id"]
    for bad in (secrets.token_urlsafe(32), None, "", 5):
        recv = FakeRecv(_line(bad, upload_id, 0, MIB) + b"x" * MIB)
        frames, _, _ = _serve(transport, recv=recv)
        assert frames == [{"type": "error", "detail": "unauthorised"}]
        assert len(recv._data) > MIB - MAX_REQUEST_BYTES - 1
    assert app.state.uploads.status(upload_id)["received"] == 0


def test_a_hub_without_a_key_still_needs_one_for_uploads(tmp_path, free):
    app = create_app(db_path=str(tmp_path / "hub.db"))
    transport = IrohTransport(token_provider=lambda: None, asgi_app=app, uploads=app.state.uploads)
    frames, _, _ = _serve(transport, _line(None, "a" * 32, 0, 1) + b"x")
    assert frames == [{"type": "error", "detail": "unauthorised"}]


def test_failed_keys_from_one_peer_do_not_block_another_peers_valid_key(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 12, peer="peer-b")["upload_id"]
    for _ in range(12):
        _serve(transport, _line(secrets.token_urlsafe(32), upload_id, 0, 1) + b"x", conn=PeerConn("peer-a"))
    blocked, _, _ = _serve(transport, _line(secrets.token_urlsafe(32), upload_id, 0, 1) + b"x", conn=PeerConn("peer-a"))
    assert blocked == [{"type": "error", "detail": "rate_limited"}]
    # The same peer, with the right key, is never refused for its own failures,
    # and another peer is untouched.
    assert _chunk(transport, token, upload_id, 0, b"aaaa", peer="peer-a")[1]["status"] == 200
    assert _chunk(transport, token, upload_id, 4, b"bbbb", peer="peer-b")[1]["status"] == 200


def test_invalid_upload_attempts_never_lock_the_owner_out_across_peers(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 3)["upload_id"]
    for n in range(iroh_api_global_limit() + 5):
        _serve(transport, _line("wrong", upload_id, 0, 1) + b"x", conn=PeerConn(f"spray-{n}"))
    assert _chunk(transport, token, upload_id, 0, b"abc", peer="owner")[1]["status"] == 200
    for n in range(iroh_api_global_limit() + 5):
        reset_auth_rate_limit(f"iroh:spray-{n}")


def iroh_api_global_limit():
    from agent_relay.core import iroh_transport

    return iroh_transport.IROH_GLOBAL_INVALID_LIMIT


def test_the_key_and_the_bytes_never_reach_the_log(hub, caplog):
    app, transport, token = hub
    payload = secrets.token_hex(16).encode()
    upload_id = _create(transport, token, len(payload), name="Board-Minutes.pdf")["upload_id"]
    with caplog.at_level(logging.DEBUG):
        _chunk(transport, token, upload_id, 0, payload)
        _serve(transport, _line("wrong-" + secrets.token_hex(4), upload_id, 0, 1) + b"x")
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in text and payload.decode() not in text and "Board-Minutes" not in text
    assert upload_id in text


# ----------------- Concurrency -----------------

def test_a_full_connection_or_hub_refuses_a_chunk_and_gives_the_slot_back(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 8)["upload_id"]
    full = iroh_api.ApiSlots(1)
    full.in_flight = 1
    frames, _, _ = _serve(transport, _line(token, upload_id, 0, 4) + b"abcd", slots=full)
    assert frames[-1] == {"type": "error", "detail": "rate_limited"}
    assert app.state.uploads.status(upload_id)["received"] == 0

    transport._api_slots.in_flight = transport._api_slots.limit
    frames, _, _ = _serve(transport, _line(token, upload_id, 0, 4) + b"abcd")
    assert frames[-1]["detail"] == "rate_limited"
    transport._api_slots.in_flight = 0
    assert _chunk(transport, token, upload_id, 0, b"abcd")[1]["status"] == 200
    assert transport._api_slots.in_flight == 0


def test_upload_limits_apply_through_the_iroh_api_calls(hub):
    app, transport, token = hub
    _create(transport, token, 1)
    _create(transport, token, 1)
    third = _api(transport, token, "POST", "/api/uploads", {"name": "a", "size": 1})[1]
    assert (third["status"], third["body"]["detail"]) == (429, "too_many_uploads")
    other_peer = _api(transport, token, "POST", "/api/uploads", {"name": "a", "size": 1}, peer="peer-b")[1]
    assert other_peer["status"] == 201
    too_big = _api(transport, token, "POST", "/api/uploads", {"name": "a", "size": 3 * 1024**3}, peer="peer-b")[1]
    assert (too_big["status"], too_big["body"]["detail"]) == (413, "too_large")


def test_the_iroh_peer_is_the_owner_of_what_it_creates(hub):
    app, transport, token = hub
    upload_id = _create(transport, token, 1, peer="peer-b")["upload_id"]
    assert app.state.uploads._uploads[upload_id].owner == "iroh:peer-b"


# ----------------- Allowlist -----------------

@pytest.mark.parametrize("method,target,template", [
    ("POST", "/api/uploads", "/api/uploads"),
    ("GET", "/api/uploads/" + "0123456789abcdef" * 2, "/api/uploads/{upload_id}"),
    ("POST", "/api/uploads/" + "0123456789abcdef" * 2 + "/finish", "/api/uploads/{upload_id}/finish"),
    ("DELETE", "/api/uploads/" + "0123456789abcdef" * 2, "/api/uploads/{upload_id}"),
])
def test_the_upload_routes_are_on_the_allowlist(method, target, template):
    assert match_allowlist(method, target)[0] == target
    assert match_allowlist(method, target)[2] == template


@pytest.mark.parametrize("method,target", [
    ("PUT", "/api/uploads/" + "a" * 32),                      # the LAN chunk route stays off iroh
    ("GET", "/api/uploads"),                                  # no listing
    ("DELETE", "/api/uploads"),
    ("POST", "/api/uploads/" + "a" * 32),
    ("GET", "/api/uploads/" + "A" * 32),                      # uppercase
    ("GET", "/api/uploads/" + "a" * 31),
    ("GET", "/api/uploads/" + "a" * 33),
    ("GET", "/api/uploads/" + "a" * 32 + "\n"),
    ("GET", "/api/uploads/" + "a" * 32 + "/"),
    ("GET", "/api/uploads/../jobs"),
    ("GET", "/api/uploads/%2e%2e"),
    ("GET", "/api/uploads/" + "a" * 32 + "?token=x"),
    ("POST", "/api/uploads?x=1"),
    ("POST", "/api/uploads/" + "a" * 32 + "/finish/x"),
    ("POST", "/api/uploads/" + "g" * 32 + "/finish"),
    ("POST", "/api/uploadsx"),
])
def test_everything_else_near_the_upload_routes_is_forbidden(method, target):
    with pytest.raises(ApiError) as refused:
        match_allowlist(method, target)
    assert refused.value.code == "forbidden_path"


def test_a_get_or_delete_with_a_body_is_a_bad_request():
    for method in ("GET", "DELETE"):
        with pytest.raises(ApiError) as refused:
            iroh_api.parse_api_request({"method": method, "path": "/api/uploads/" + "a" * 32, "body": {"x": 1}})
        assert refused.value.code == "bad_request"


def test_the_exact_allowlist_additions_are_these():
    added = [entry for entry in iroh_api.API_ALLOWLIST if entry[1].startswith("/api/uploads")]
    assert added == [
        ("POST", "/api/uploads", False),
        ("GET", "/api/uploads/{upload_id}", False),
        ("POST", "/api/uploads/{upload_id}/finish", False),
        ("DELETE", "/api/uploads/{upload_id}", False),
    ]


def test_the_published_spec_lists_the_upload_op_and_limits():
    from pathlib import Path

    spec_path = Path(__file__).resolve().parent.parent / "docs" / "mobile-api-spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    iroh = spec["x-agnview-iroh"]
    assert "uploads" in iroh["hello_capabilities"]
    assert iroh["limits"]["upload_chunk_bytes"] == iroh_api.MAX_UPLOAD_CHUNK_BYTES == uploads.CHUNK_SIZE
    assert iroh["limits"]["upload_body_timeout_seconds"] == iroh_api.UPLOAD_BODY_TIMEOUT_SECONDS
    assert "upload_chunk" in iroh["description"]


# ----------------- Switches and capabilities -----------------

def test_a_hub_advertises_uploads_only_when_they_are_on_for_iroh(tmp_path, free, monkeypatch):
    on = create_app(db_path=str(tmp_path / "a.db"))
    assert on.state.iroh.capabilities == ["console", "api", "uploads"]
    assert on.state.iroh.status()["capabilities"] == ["console", "api", "uploads"]

    monkeypatch.setenv("AGNVIEW_IROH_UPLOADS", "0")
    off = create_app(db_path=str(tmp_path / "b.db"))
    assert off.state.iroh.capabilities == ["console", "api"]
    assert off.state.iroh_uploads_enabled is False
    assert off.state.uploads is not None  # the LAN still has them


def test_the_config_switch_for_iroh_uploads(tmp_path, free, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("iroh_uploads_enabled: false\n", encoding="utf-8")
    monkeypatch.setenv("AGNVIEW_CONFIG", str(config))
    app = create_app(db_path=str(tmp_path / "hub.db"))
    assert app.state.iroh.capabilities == ["console", "api"]
    assert app.state.uploads is not None


def test_iroh_uploads_are_refused_when_off_even_if_asked_directly(tmp_path, free, monkeypatch):
    monkeypatch.setenv("AGNVIEW_IROH_UPLOADS", "0")
    token = _fake_token()
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)
    transport = app.state.iroh
    api_call = _api(transport, token, "POST", "/api/uploads", {"name": "a", "size": 1})
    assert api_call[-1] == {"type": "error", "detail": "forbidden_path"}
    frames, _, _ = _serve(transport, _line(token, "a" * 32, 0, 1) + b"x")
    assert frames[-1] == {"type": "error", "detail": "forbidden_path"}


def test_with_the_iroh_api_off_there_are_no_uploads_over_iroh(tmp_path, free, monkeypatch):
    monkeypatch.setenv("AGNVIEW_IROH_API", "0")
    app = create_app(db_path=str(tmp_path / "hub.db"))
    assert app.state.iroh.capabilities == ["console"]


def test_a_transport_with_no_upload_manager_has_no_uploads_capability():
    transport = IrohTransport(token_provider=lambda: "t", asgi_app=object())
    assert transport.capabilities == ["console", "api"]


# ----------------- Dispatch and jobs take the returned path -----------------

def test_the_path_from_finish_is_accepted_by_dispatch_and_jobs_over_iroh(hub):
    app, transport, token = hub
    data = b"attachment"
    upload_id = _create(transport, token, len(data))["upload_id"]
    _chunk(transport, token, upload_id, 0, data)
    path = _api(transport, token, "POST", f"/api/uploads/{upload_id}/finish")[1]["body"]["path"]

    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)

    app.state.engine.runner.dispatch = fake_dispatch
    request = {"token": token, "op": "api", "method": "POST", "path": "/api/console/dispatch",
               "body": {"agent": "codex", "prompt": "look", "files": [path]}}

    async def run():
        bi = FakeBi(request)
        await transport._handle_stream(PeerConn(), bi, iroh_api.ApiSlots(4))
        # Let the dispatch task the route created run before the loop closes.
        await asyncio.sleep(0.05)
        return bi._send.frames()

    frames = asyncio.run(run())
    assert frames[1]["status"] == 200
    assert calls[0]["files"] == [path]


def test_a_path_outside_the_uploads_folder_is_refused_over_iroh(hub, tmp_path):
    app, transport, token = hub
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)

    app.state.engine.runner.dispatch = fake_dispatch
    frames = _api(transport, token, "POST", "/api/console/dispatch",
                  {"agent": "codex", "prompt": "look", "files": [str(outside)]})
    assert (frames[1]["status"], frames[1]["body"]["detail"]) == (422, "forbidden_file")
    assert calls == []
