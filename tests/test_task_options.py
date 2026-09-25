"""Per-task model, effort and files on pipeline jobs, and the file rule for iroh.

Paths are generated per run under a temporary directory. A request that arrives
over iroh is built by hand in-process, with the client address the transport
gives it, so no port is opened and no agent runs.
"""

import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core import dispatch_guard, iroh_api
from agent_relay.core.capabilities import EFFORTS_BY_PROVIDER, MODELS, validate_task_options
from agent_relay.core.dispatch_guard import DispatchRefused, check_files
from agent_relay.core.prompts import append_files_context


def _app(tmp_path):
    return create_app(db_path=str(tmp_path / "hub.db"))


def _task(**over):
    task = {"id": "t1", "title": "Do the thing", "assigned_agent": "codex"}
    task.update(over)
    return task


def _job(**task_over):
    return {"title": "Real job", "tasks": [_task(**task_over)]}


def _iroh_call(app, method, path, body=None):
    """One request through the whole app, as the iroh transport hands it in."""
    request = iroh_api.ApiRequest(
        method=method, path=path, query="", template=path,
        body=None if body is None else json.dumps(body).encode("utf-8"),
    )
    frame = asyncio.run(iroh_api.forward_to_app(app, request, token=None))
    return frame["status"], frame["body"]


# ----------------- Model and effort -----------------

def test_a_task_keeps_its_model_effort_and_files(tmp_path):
    client = TestClient(_app(tmp_path))
    model = MODELS["codex"][0]["id"]
    body = _job(model=model, effort="high", files=["notes.txt"])
    created = client.post("/api/jobs", json=body)
    assert created.status_code == 200
    task = created.json()["tasks"]["t1"]
    assert (task["model"], task["effort"], task["files"]) == (model, "high", ["notes.txt"])

    listed = client.get("/api/jobs").json()[0]["tasks"]["t1"]
    assert (listed["model"], listed["effort"], listed["files"]) == (model, "high", ["notes.txt"])
    fetched = client.get(f"/api/jobs/{created.json()['id']}").json()["tasks"]["t1"]
    assert fetched["model"] == model


def test_a_client_that_omits_the_fields_behaves_as_before(tmp_path):
    client = TestClient(_app(tmp_path))
    task = client.post("/api/jobs", json=_job()).json()["tasks"]["t1"]
    assert (task["model"], task["effort"], task["files"]) == (None, None, None)


@pytest.mark.parametrize("field,value", [
    ("model", "not-a-real-model"),
    ("model", "--dangerously-skip-permissions"),
    ("effort", "ludicrous"),
    ("effort", "high; rm -rf x"),
])
def test_an_unknown_model_or_effort_is_refused_with_422(tmp_path, field, value):
    client = TestClient(_app(tmp_path))
    res = client.post("/api/jobs", json=_job(**{field: value}))
    assert res.status_code == 422
    assert client.get("/api/jobs").json() == []


def test_a_model_from_another_agents_list_is_refused(tmp_path):
    client = TestClient(_app(tmp_path))
    claude_only = MODELS["claude_code"][0]["id"]
    assert claude_only not in {m["id"] for m in MODELS["codex"]}
    assert client.post("/api/jobs", json=_job(model=claude_only)).status_code == 422
    assert client.post(
        "/api/jobs", json=_job(assigned_agent="claude_code", model=claude_only)
    ).status_code == 200


def test_an_instance_suffix_selects_the_same_lists(tmp_path):
    validate_task_options("t", "codex@laptop", MODELS["codex"][0]["id"], "low")
    with pytest.raises(ValueError):
        validate_task_options("t", "codex@laptop", "nope", None)


def test_the_neutral_values_the_runner_ignores_are_accepted():
    validate_task_options("t", "codex", "auto", "default")


def test_an_effort_is_checked_against_the_agents_own_list():
    only_claude = {e["id"] for e in EFFORTS_BY_PROVIDER["claude_code"]} - {
        e["id"] for e in EFFORTS_BY_PROVIDER["antigravity"]
    }
    assert only_claude
    with pytest.raises(ValueError):
        validate_task_options("t", "antigravity", None, sorted(only_claude)[0])


def test_the_capabilities_route_serves_the_same_lists(tmp_path):
    caps = TestClient(_app(tmp_path)).get("/api/system/capabilities").json()
    assert caps["models"] == MODELS
    assert caps["efforts_by_provider"] == EFFORTS_BY_PROVIDER


# ----------------- Shape limits on files -----------------

def test_too_many_files_or_a_control_character_is_refused(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.post("/api/jobs", json=_job(files=[f"f{i}" for i in range(33)])).status_code == 422
    assert client.post("/api/jobs", json=_job(files=["a\nb"])).status_code == 422
    assert client.post("/api/jobs", json=_job(files=[""])).status_code == 422
    assert client.post("/api/jobs", json=_job(files=["x" * 1025])).status_code == 422


# ----------------- The runner passes them through -----------------

def test_dispatch_names_files_the_same_way_the_task_prompt_does():
    assert append_files_context("hi", None) == "hi"
    assert append_files_context("hi", []) == "hi"
    assert append_files_context("hi", ["/a", "/b"]) == "hi\n[Context Files: /a, /b]"


def test_the_runner_hands_model_effort_and_files_to_the_agent(tmp_path):
    app = _app(tmp_path)
    runner = app.state.engine.runner
    seen = {}

    async def fake_codex(prompt, cwd, session_id, model=None, effort=None):
        seen.update(prompt=prompt, model=model, effort=effort)

    async def fake_adapter(adapter, prompt, cwd, session_id, model=None, effort=None, skill=None):
        seen.update(prompt=prompt, model=model, effort=effort)

    runner._run_codex = fake_codex
    runner._run_adapter = fake_adapter
    asyncio.run(runner.dispatch(
        agent="codex", prompt="hello", cwd=str(tmp_path), session_id="s",
        model="o3", effort="high", files=["/x/y.txt"],
    ))
    assert seen == {"prompt": "hello\n[Context Files: /x/y.txt]", "model": "o3", "effort": "high"}


def test_the_task_prompt_carries_the_options_and_files(tmp_path):
    client = TestClient(_app(tmp_path))
    job = client.post("/api/jobs", json=_job(model="o3", effort="low", files=["/x/y.txt"])).json()
    prompt = client.get("/api/tasks/t1/web-prompt").json()["prompt"]
    assert "Requested model: `o3`" in prompt
    assert "Requested effort: `low`" in prompt
    assert "[Context Files: /x/y.txt]" in prompt
    plain = TestClient(_app(tmp_path / "other"))
    plain.post("/api/jobs", json=_job())
    assert "Run Options" not in plain.get("/api/tasks/t1/web-prompt").json()["prompt"]
    assert job["id"]


def test_the_mcp_claim_reply_carries_them(tmp_path):
    from agent_relay.mcp.server import AgentRelayMCPServer

    app = _app(tmp_path)
    TestClient(app).post("/api/jobs", json=_job(model="o3", files=["/x/y.txt"]))
    server = AgentRelayMCPServer.__new__(AgentRelayMCPServer)
    server.engine = app.state.engine
    reply = json.loads(server.handle_tool_call("relay_claim_task", {"task_id": "t1", "agent": "codex"}))
    assert reply["model"] == "o3" and reply["files"] == ["/x/y.txt"] and reply["effort"] is None


# ----------------- Files over iroh -----------------

def _uploads_root(tmp_path):
    root = tmp_path / "phone-uploads"
    root.mkdir()
    return root


def _make_upload(root, name="photo.txt", upload_id="a" * 32, data=b"x"):
    folder = root / upload_id
    folder.mkdir()
    (folder / name).write_bytes(data)
    (folder / ".meta.json").write_text("{}")
    return folder / name


def test_an_upload_path_is_accepted_and_returned_as_a_real_path(tmp_path):
    root = _uploads_root(tmp_path)
    stored = _make_upload(root)
    assert check_files([str(stored)], None, [], str(root)) == [os.path.realpath(stored)]


@pytest.mark.parametrize("build", [
    lambda root: str(root / ("a" * 32) / ".meta.json"),            # the hub's own bookkeeping
    lambda root: str(root / ("b" * 32) / "missing.txt"),           # nothing there
    lambda root: str(root / "not-an-id" / "photo.txt"),            # not an upload folder
    lambda root: str(root / ("a" * 32) / "sub" / "photo.txt"),     # too deep
    lambda root: str(root / ("a" * 32) / ".." / ("a" * 32) / ".meta.json"),
    lambda root: str(root),                                        # the folder itself
])
def test_anything_else_under_the_uploads_folder_is_refused(tmp_path, build):
    root = _uploads_root(tmp_path)
    _make_upload(root)
    with pytest.raises(DispatchRefused) as refused:
        check_files([build(root)], None, [], str(root))
    assert refused.value.detail == "forbidden_file"


@pytest.mark.parametrize("entry", [
    "", "a\x00b", "a\nb", "\\\\host\\share\\x.txt", "//host/share/x.txt", 5, None,
])
def test_malformed_file_entries_are_refused(tmp_path, entry):
    root = _uploads_root(tmp_path)
    with pytest.raises(DispatchRefused):
        check_files([entry], str(tmp_path), [], str(root))


def test_project_files_need_a_folder_and_must_be_ones_the_listing_shows(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "node_modules").mkdir()
    ok = project / "src" / "main.py"
    ok.write_text("x")
    for hidden in (project / ".env", project / ".git" / "config", project / "node_modules" / "a.js",
                   project / "data.db", project / "run.log", project / "shot.png"):
        hidden.write_text("x")
    cwd = str(project)

    assert check_files(["src/main.py"], cwd, [], None) == [os.path.realpath(ok)]
    for entry in (".env", ".git/config", "node_modules/a.js", "data.db", "run.log", "shot.png",
                  "src", "missing.py"):
        with pytest.raises(DispatchRefused):
            check_files([entry], cwd, [], None)
    # With no folder a relative name means nothing.
    with pytest.raises(DispatchRefused):
        check_files(["src/main.py"], None, [], None)


def test_a_path_that_climbs_out_or_names_another_folder_is_refused(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    for entry in ("../secret.txt", str(secret), str(project / ".." / "secret.txt")):
        with pytest.raises(DispatchRefused):
            check_files([entry], str(project), [], None)


def test_a_symlink_out_of_the_folder_is_refused(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    link = project / "link.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot make symlinks")
    with pytest.raises(DispatchRefused):
        check_files(["link.txt"], str(project), [], None)


def test_a_symlink_into_the_uploads_folder_needs_no_special_case(tmp_path):
    root = _uploads_root(tmp_path)
    stored = _make_upload(root)
    project = tmp_path / "project"
    project.mkdir()
    link = project / "up.txt"
    try:
        link.symlink_to(stored)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot make symlinks")
    assert check_files(["up.txt"], str(project), [], str(root)) == [os.path.realpath(stored)]


def test_a_task_without_a_folder_may_use_files_under_a_dispatch_root(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    ok = root / "notes.md"
    ok.write_text("x")
    assert check_files([str(ok)], None, [str(root)], None) == [os.path.realpath(ok)]
    with pytest.raises(DispatchRefused):
        check_files([str(tmp_path / "other.md")], None, [str(root)], None)


def test_the_listing_route_and_the_guard_share_one_rule():
    assert dispatch_guard.listing_shows_file("main.py")
    assert not dispatch_guard.listing_shows_file(".env")
    assert not dispatch_guard.listing_shows_file("x.db")
    assert not dispatch_guard.listing_shows_dir(".ssh")
    assert not dispatch_guard.listing_shows_dir("node_modules")


def test_the_system_files_listing_is_unchanged(tmp_path):
    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / ".git").mkdir()
    (project / "src" / "main.py").write_text("x")
    (project / ".env").write_text("x")
    (project / "a.db").write_text("x")
    (project / ".git" / "config").write_text("x")
    listing = TestClient(_app(tmp_path)).get("/api/system/files", params={"cwd": str(project)}).json()
    assert listing["files"] == ["src/main.py"]


def test_a_dispatch_over_iroh_refuses_a_file_outside_the_uploads_and_the_project(tmp_path):
    app = _app(tmp_path)
    root = _uploads_root(tmp_path)
    app.state.uploads_dir = str(root)
    project = tmp_path / "project"
    project.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)

    app.state.engine.runner.dispatch = fake_dispatch

    def dispatch(files):
        return _iroh_call(app, "POST", "/api/console/dispatch", {
            "agent": "codex", "prompt": "hi", "working_directory": str(project), "files": files,
        })

    status, body = dispatch([str(secret)])
    assert (status, body["detail"]) == (422, "forbidden_file")
    assert calls == []

    stored = _make_upload(root)
    status, body = dispatch([str(stored)])
    assert status == 200
    assert calls[0]["files"] == [os.path.realpath(stored)]


def test_a_dispatch_on_the_lan_is_unchanged(tmp_path):
    app = _app(tmp_path)
    calls = []

    async def fake_dispatch(**kwargs):
        calls.append(kwargs)

    app.state.engine.runner.dispatch = fake_dispatch
    with TestClient(app) as client:
        res = client.post("/api/console/dispatch", json={
            "agent": "codex", "prompt": "hi", "files": ["/anywhere/at/all.txt"],
        })
    assert res.status_code == 200
    assert calls[0]["files"] == ["/anywhere/at/all.txt"]


def test_a_job_over_iroh_refuses_a_file_it_may_not_name(tmp_path):
    app = _app(tmp_path)
    app.state.uploads_dir = str(_uploads_root(tmp_path))
    status, body = _iroh_call(app, "POST", "/api/jobs", _job(files=[str(tmp_path / "secret.txt")]))
    assert (status, body["detail"]) == (422, "forbidden_file")
    assert TestClient(app).get("/api/jobs").json() == []


def test_a_job_over_iroh_accepts_an_upload_and_stores_the_real_path(tmp_path):
    app = _app(tmp_path)
    root = _uploads_root(tmp_path)
    app.state.uploads_dir = str(root)
    stored = _make_upload(root)
    status, body = _iroh_call(app, "POST", "/api/jobs", _job(files=[str(stored)]))
    assert status == 200
    assert body["tasks"]["t1"]["files"] == [os.path.realpath(stored)]


def test_a_job_over_iroh_still_validates_model_and_effort(tmp_path):
    status, _ = _iroh_call(_app(tmp_path), "POST", "/api/jobs", _job(model="nope"))
    assert status == 422


def test_a_job_on_the_lan_keeps_any_file_it_names(tmp_path):
    client = TestClient(_app(tmp_path))
    res = client.post("/api/jobs", json=_job(files=["/anywhere/at/all.txt"]))
    assert res.status_code == 200
    assert res.json()["tasks"]["t1"]["files"] == ["/anywhere/at/all.txt"]
