"""Creating and deleting pipelines from a phone over iroh.

POST /api/jobs and DELETE /api/jobs/{id} are on the iroh allowlist. They pass
the same key check, limits and per-peer limiter as every other iroh call, and
the file and model rules a dispatch has. Streams are in memory and every key
and path is generated per run.
"""

import asyncio
import json
import logging
import secrets

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core import iroh_api, uploads
from agent_relay.core.capabilities import MODELS
from agent_relay.core.iroh_api import ApiError, match_allowlist
from agent_relay.core.iroh_transport import MAX_REQUEST_BYTES, IrohTransport
from agent_relay.core.pairing import reset_auth_rate_limit
from test_iroh_api import FakeBi, FakeRecv, _fake_token
from test_iroh_uploads import PeerConn

PEERS = ("peer-a", "peer-b", "unknown")


@pytest.fixture(autouse=True)
def clean_limiter():
    keys = ["iroh"] + [f"iroh:{peer}" for peer in PEERS]
    for key in keys:
        reset_auth_rate_limit(key)
    yield
    for key in keys:
        reset_auth_rate_limit(key)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "free_bytes", lambda path: 500 * 1024**3)
    token = _fake_token()
    app = create_app(db_path=str(tmp_path / "hub.db"), auth_token=token)
    transport = IrohTransport(
        token_provider=lambda: app.state.auth_token,
        asgi_app=app,
        uploads=app.state.uploads,
    )
    return app, transport, token


def _call(transport, token, method, path, body=None, peer="peer-a", slots=None):
    request = {"token": token, "op": "api", "method": method, "path": path, "body": body}
    bi = FakeBi(request)
    conn = PeerConn(peer)
    asyncio.run(transport._handle_stream(conn, bi, slots or iroh_api.ApiSlots(4)))
    return bi._send.frames(), conn


def _jobs(app):
    return TestClient(app, client=("203.0.113.7", 1)).get(
        "/api/jobs", headers={"X-AgnView-Token": app.state.auth_token}
    ).json()


def _task(**over):
    task = {"id": "t1", "title": "Do the thing", "assigned_agent": "codex"}
    task.update(over)
    return task


def _job(**over):
    job = {"title": "Phone job", "tasks": [_task()]}
    job.update(over)
    return job


def _finished_upload(app):
    manager = app.state.uploads
    created = manager.create("notes.txt", 5, None, "iroh:peer-a")
    manager.write_chunk(created["upload_id"], 0, b"hello")
    return manager.finish(created["upload_id"])["path"]


# ----------------- Allowlist -----------------

def test_the_two_job_routes_are_on_the_allowlist():
    assert match_allowlist("POST", "/api/jobs") == ("/api/jobs", "", "/api/jobs")
    assert match_allowlist("DELETE", "/api/jobs/job-1a2b3c4d")[2] == "/api/jobs/{id}"
    added = [entry for entry in iroh_api.API_ALLOWLIST if entry[1].startswith("/api/jobs")]
    assert added == [
        ("GET", "/api/jobs", False),
        ("GET", "/api/jobs/{id}", False),
        ("POST", "/api/jobs", False),
        ("DELETE", "/api/jobs/{id}", False),
    ]


@pytest.mark.parametrize("method,target", [
    ("DELETE", "/api/jobs"),
    ("PUT", "/api/jobs"),
    ("PUT", "/api/jobs/job-1"),
    ("PATCH", "/api/jobs/job-1"),
    ("POST", "/api/jobs/job-1"),
    ("DELETE", "/api/jobs/job-1/tasks"),
    ("DELETE", "/api/jobs/"),
    ("DELETE", "/api/jobs/.."),
    ("DELETE", "/api/jobs/../mobile/pairing"),
    ("DELETE", "/api/jobs/%2e%2e"),
    ("DELETE", "/api/jobs/.hidden"),
    ("DELETE", "/api/jobs/" + "a" * 129),
    ("DELETE", "/api/jobs/job-1\n"),
    ("DELETE", "/api/jobs/job 1"),
    ("DELETE", "/api/jobs/job-1?x=1"),
    ("POST", "/api/jobs?x=1"),
    ("POST", "/api/jobs/"),
    ("POST", "//api/jobs"),
])
def test_everything_else_near_the_job_routes_stays_forbidden(method, target):
    with pytest.raises(ApiError) as refused:
        match_allowlist(method, target)
    assert refused.value.code == "forbidden_path"


def test_a_delete_with_a_body_is_a_bad_request():
    with pytest.raises(ApiError) as refused:
        iroh_api.parse_api_request({"method": "DELETE", "path": "/api/jobs/job-1", "body": {}})
    assert refused.value.code == "bad_request"


# ----------------- Create -----------------

def test_a_pipeline_can_be_created_and_deleted_over_iroh(hub):
    app, transport, token = hub
    path = _finished_upload(app)
    model = MODELS["codex"][0]["id"]
    body = _job(tasks=[_task(model=model, effort="high", files=[path]),
                       _task(id="t2", title="Then this", dependencies=["t1"])])

    frames, conn = _call(transport, token, "POST", "/api/jobs", body)
    assert [f["type"] for f in frames] == ["hello", "response"]
    assert frames[1]["status"] == 200
    job = frames[1]["body"]
    assert job["tasks"]["t1"]["model"] == model and job["tasks"]["t1"]["effort"] == "high"
    assert job["tasks"]["t1"]["files"] == [path]
    assert not conn.closed

    listed, _ = _call(transport, token, "GET", "/api/jobs")
    assert [j["id"] for j in listed[1]["body"]] == [job["id"]]

    deleted, _ = _call(transport, token, "DELETE", f"/api/jobs/{job['id']}")
    assert (deleted[1]["status"], deleted[0]["type"]) == (200, "hello")
    assert _jobs(app) == []


def test_a_job_the_phone_names_can_be_fetched_and_deleted_by_that_name(hub):
    app, transport, token = hub
    frames, _ = _call(transport, token, "POST", "/api/jobs", _job(id="phone-job_1.a"))
    assert frames[1]["body"]["id"] == "phone-job_1.a"
    assert _call(transport, token, "GET", "/api/jobs/phone-job_1.a")[0][1]["status"] == 200
    assert _call(transport, token, "DELETE", "/api/jobs/phone-job_1.a")[0][1]["status"] == 200


@pytest.mark.parametrize("job_id", ["../x", "a b", ".hidden", "a/b", "x" * 129, "job\n1", "%2e%2e", ""])
def test_a_job_id_the_phone_could_not_address_again_is_refused(hub, job_id):
    app, transport, token = hub
    frames, _ = _call(transport, token, "POST", "/api/jobs", _job(id=job_id))
    assert (frames[1]["status"], frames[1]["body"]["detail"]) == (422, "invalid_job_id")
    assert _jobs(app) == []


@pytest.mark.parametrize("task_id", ["a/b", "a b", ".x", "y" * 129, ""])
def test_a_task_id_the_phone_could_not_address_again_is_refused(hub, task_id):
    app, transport, token = hub
    frames, _ = _call(transport, token, "POST", "/api/jobs", _job(tasks=[_task(id=task_id)]))
    assert (frames[1]["status"], frames[1]["body"]["detail"]) == (422, "invalid_task_id")
    assert _jobs(app) == []


def test_the_lan_still_accepts_any_id(tmp_path):
    app = create_app(db_path=str(tmp_path / "hub.db"))
    client = TestClient(app)
    res = client.post("/api/jobs", json=_job(id="has space", tasks=[_task(id="a/b")]))
    assert res.status_code == 200 and res.json()["id"] == "has space"


def test_a_create_naming_a_file_it_may_not_is_refused_and_creates_nothing(hub, tmp_path):
    app, transport, token = hub
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    for entry in (str(secret), "../secret.txt", "relative.txt", str(tmp_path)):
        frames, _ = _call(transport, token, "POST", "/api/jobs", _job(tasks=[_task(files=[entry])]))
        assert (frames[1]["status"], frames[1]["body"]["detail"]) == (422, "forbidden_file"), entry
    assert _jobs(app) == []


def test_one_forbidden_file_among_good_ones_refuses_the_whole_job(hub, tmp_path):
    app, transport, token = hub
    good = _finished_upload(app)
    frames, _ = _call(transport, token, "POST", "/api/jobs", _job(tasks=[
        _task(files=[good]), _task(id="t2", files=[good, str(tmp_path / "nope.txt")]),
    ]))
    assert frames[1]["body"]["detail"] == "forbidden_file"
    assert _jobs(app) == []


def test_a_create_under_a_dispatch_root_may_name_a_listed_file(hub, tmp_path):
    app, transport, token = hub
    root = tmp_path / "work"
    root.mkdir()
    listed = root / "spec.md"
    listed.write_text("x")
    (root / ".env").write_text("x")
    app.state.config.iroh_dispatch_roots = [str(root)]
    ok, _ = _call(transport, token, "POST", "/api/jobs", _job(tasks=[_task(files=[str(listed)])]))
    assert ok[1]["status"] == 200
    hidden, _ = _call(transport, token, "POST", "/api/jobs", _job(tasks=[_task(files=[str(root / ".env")])]))
    assert hidden[1]["body"]["detail"] == "forbidden_file"


@pytest.mark.parametrize("change", [
    {"model": "not-a-real-model"},
    {"model": "--dangerously-skip-permissions"},
    {"effort": "ludicrous"},
    {"assigned_agent": "claude_code", "model": MODELS["codex"][0]["id"]},
])
def test_an_unknown_model_or_effort_is_refused_over_iroh(hub, change):
    app, transport, token = hub
    frames, _ = _call(transport, token, "POST", "/api/jobs", _job(tasks=[_task(**change)]))
    assert frames[1]["status"] == 422
    assert _jobs(app) == []


def test_a_malformed_job_is_a_400_or_422_response_not_a_crash(hub):
    app, transport, token = hub
    for body in ({"title": "", "tasks": [_task()]}, {"title": "x", "tasks": []}, {"title": "x"}, [1, 2]):
        frames, conn = _call(transport, token, "POST", "/api/jobs", body)
        assert frames[1]["type"] == "response" and frames[1]["status"] in (400, 422), body
        assert not conn.closed
    assert _jobs(app) == []


# ----------------- Delete -----------------

def test_deleting_a_missing_job_is_a_404_response(hub):
    app, transport, token = hub
    frames, conn = _call(transport, token, "DELETE", "/api/jobs/job-does-not-exist")
    assert [f["type"] for f in frames] == ["hello", "response"]
    assert frames[1]["status"] == 404
    assert "not found" in frames[1]["body"]["detail"].lower()
    assert not conn.closed


def test_a_delete_removes_only_the_named_job(hub):
    app, transport, token = hub
    first = _call(transport, token, "POST", "/api/jobs", _job(id="keep"))[0][1]["body"]["id"]
    second = _call(transport, token, "POST", "/api/jobs", _job(id="drop"))[0][1]["body"]["id"]
    _call(transport, token, "DELETE", f"/api/jobs/{second}")
    assert [j["id"] for j in _jobs(app)] == [first]


# ----------------- Size -----------------

def _sized_job(target_bytes):
    tasks, size, n = [], 0, 0
    while size < target_bytes:
        task = _task(id=f"t{n}", title="step", description="x" * 900)
        tasks.append(task)
        size += len(json.dumps(task)) + 2
        n += 1
    return _job(tasks=tasks)


def test_a_large_pipeline_over_the_request_line_limit_gets_a_clear_error_and_no_crash(hub):
    app, transport, token = hub
    body = _sized_job(MAX_REQUEST_BYTES + 5000)
    frames, conn = _call(transport, token, "POST", "/api/jobs", body)
    assert frames == [{"type": "error", "detail": "too_large"}]
    assert conn.closed
    assert _jobs(app) == []
    # The hub carries on: the next connection is served as usual.
    assert _call(transport, token, "POST", "/api/jobs", _job())[0][1]["status"] == 200


def test_a_pipeline_just_under_the_limit_is_created(hub):
    app, transport, token = hub
    body = _sized_job(MAX_REQUEST_BYTES - 6000)
    raw = json.dumps({"token": token, "op": "api", "method": "POST", "path": "/api/jobs", "body": body})
    assert len(raw) < MAX_REQUEST_BYTES
    frames, _ = _call(transport, token, "POST", "/api/jobs", body)
    assert frames[1]["status"] == 200
    assert len(_jobs(app)[0]["tasks"]) == len(body["tasks"])


def test_a_request_that_never_ends_is_not_a_create(hub, monkeypatch):
    app, transport, token = hub
    from agent_relay.core import iroh_transport

    monkeypatch.setattr(iroh_transport, "REQUEST_READ_TIMEOUT_SECONDS", 0.05)
    bi = FakeBi(b"")
    bi._recv = FakeRecv(b"", hang=True)
    asyncio.run(transport._handle_stream(PeerConn(), bi, iroh_api.ApiSlots(4)))
    assert bi._send.frames() == [{"type": "error", "detail": "timeout"}]
    assert _jobs(app) == []


# ----------------- Auth and limits -----------------

def test_a_wrong_or_missing_key_creates_and_deletes_nothing(hub):
    app, transport, token = hub
    job_id = _call(transport, token, "POST", "/api/jobs", _job(id="mine"))[0][1]["body"]["id"]
    for bad in (secrets.token_urlsafe(32), None, "", 5):
        frames, conn = _call(transport, bad, "POST", "/api/jobs", _job(id="theirs"))
        assert frames == [{"type": "error", "detail": "unauthorised"}]
        assert conn.closed
        frames, _ = _call(transport, bad, "DELETE", f"/api/jobs/{job_id}")
        assert frames == [{"type": "error", "detail": "unauthorised"}]
    assert [j["id"] for j in _jobs(app)] == ["mine"]


def test_a_hub_without_a_key_still_needs_one_for_the_job_routes(tmp_path):
    app = create_app(db_path=str(tmp_path / "hub.db"))
    transport = IrohTransport(token_provider=lambda: None, asgi_app=app)
    frames, _ = _call(transport, None, "POST", "/api/jobs", _job())
    assert frames == [{"type": "error", "detail": "unauthorised"}]
    assert TestClient(app).get("/api/jobs").json() == []


def test_another_peers_wrong_keys_do_not_block_a_valid_create(hub):
    app, transport, token = hub
    for _ in range(12):
        _call(transport, secrets.token_urlsafe(32), "POST", "/api/jobs", _job(), peer="peer-a")
    blocked, _ = _call(transport, secrets.token_urlsafe(32), "POST", "/api/jobs", _job(), peer="peer-a")
    assert blocked == [{"type": "error", "detail": "rate_limited"}]
    assert _call(transport, token, "POST", "/api/jobs", _job(id="a"), peer="peer-a")[0][1]["status"] == 200
    assert _call(transport, token, "POST", "/api/jobs", _job(id="b"), peer="peer-b")[0][1]["status"] == 200


def test_a_full_connection_or_hub_refuses_a_create_and_a_delete(hub):
    app, transport, token = hub
    full = iroh_api.ApiSlots(1)
    full.in_flight = 1
    for method, path, body in (("POST", "/api/jobs", _job()), ("DELETE", "/api/jobs/x", None)):
        frames, _ = _call(transport, token, method, path, body, slots=full)
        assert frames[-1] == {"type": "error", "detail": "rate_limited"}
    transport._api_slots.in_flight = transport._api_slots.limit
    frames, _ = _call(transport, token, "POST", "/api/jobs", _job())
    assert frames[-1]["detail"] == "rate_limited"
    transport._api_slots.in_flight = 0
    assert _jobs(app) == []


def test_the_job_body_and_key_never_reach_the_log(hub, caplog):
    app, transport, token = hub
    title = "Quarterly-Plan-" + secrets.token_hex(4)
    with caplog.at_level(logging.DEBUG):
        _call(transport, token, "POST", "/api/jobs", _job(title=title))
        _call(transport, "wrong-" + secrets.token_hex(4), "POST", "/api/jobs", _job(title=title))
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert title not in text and token not in text
    assert "POST /api/jobs" in text


# ----------------- GET is unchanged -----------------

def test_get_jobs_over_iroh_is_unchanged(hub):
    app, transport, token = hub
    empty = _call(transport, token, "GET", "/api/jobs")[0]
    assert empty[1] == {"type": "response", "status": 200, "body": []}
    created = _call(transport, token, "POST", "/api/jobs", _job(id="one"))[0][1]["body"]
    listed = _call(transport, token, "GET", "/api/jobs")[0][1]
    one = _call(transport, token, "GET", "/api/jobs/one")[0][1]
    assert listed["status"] == 200 and listed["body"] == [created]
    assert one["status"] == 200 and one["body"] == created
    missing = _call(transport, token, "GET", "/api/jobs/nope")[0][1]
    assert missing["status"] == 404
    # A query on the jobs routes is still refused, as before.
    assert _call(transport, token, "GET", "/api/jobs?limit=5")[0][-1] == {
        "type": "error", "detail": "forbidden_path"}
    # A GET with a body is still a bad request.
    assert _call(transport, token, "GET", "/api/jobs", body={"x": 1})[0][-1] == {
        "type": "error", "detail": "bad_request"}
