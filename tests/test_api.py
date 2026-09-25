"""Integration tests for AgentRelay REST API."""

import pytest
from fastapi.testclient import TestClient
from agent_relay.api.app import create_app


@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "api_test.db")
    app = create_app(db_path=db_file)
    return TestClient(app)


def test_api_job_and_task_lifecycle(client):
    # 1. Create Job
    job_payload = {
        "id": "job-api-test",
        "title": "API Lifecycle Test",
        "description": "Testing full API flow",
        "tasks": [
            {
                "id": "task-api-A",
                "title": "Backend",
                "assigned_agent": "codex",
                "dependencies": []
            },
            {
                "id": "task-api-B",
                "title": "Frontend",
                "assigned_agent": "claude_code",
                "dependencies": ["task-api-A"]
            }
        ]
    }
    res = client.post("/api/jobs", json=job_payload)
    assert res.status_code == 200
    job_data = res.json()
    assert job_data["id"] == "job-api-test"

    # 2. Get Wait status for Task B (should not be ready)
    res_b = client.get("/api/tasks/task-api-B/wait-status")
    assert res_b.status_code == 200
    assert res_b.json()["ready"] is False
    assert "task-api-A" in res_b.json()["unmet_dependencies"]

    # 3. Claim Task A
    res_claim = client.post("/api/tasks/task-api-A/claim", json={"agent": "codex"})
    assert res_claim.status_code == 200
    assert res_claim.json()["status"] == "in_progress"

    # 4. Complete Task A
    res_comp = client.post("/api/tasks/task-api-A/complete", json={
        "summary": "Completed backend endpoints",
        "artifacts": ["server.py"]
    })
    assert res_comp.status_code == 200
    assert res_comp.json()["status"] == "completed"

    # 5. Task B should now be ready!
    res_b_ready = client.get("/api/tasks/task-api-B/wait-status")
    assert res_b_ready.status_code == 200
    assert res_b_ready.json()["ready"] is True
    assert res_b_ready.json()["upstream_summaries"]["task-api-A"]["output_summary"] == "Completed backend endpoints"

    # 6. Generate Web Prompt for Task B (Claude.ai & Gemini)
    res_prompt = client.get("/api/tasks/task-api-B/web-prompt?target_llm=claude.ai")
    assert res_prompt.status_code == 200
    assert "Completed backend endpoints" in res_prompt.json()["prompt"]

    res_gemini = client.get("/api/tasks/task-api-B/web-prompt?target_llm=gemini")
    assert res_gemini.status_code == 200
    assert "Google Gemini (gemini.google.com)" in res_gemini.json()["prompt"]
    assert "Completed backend endpoints" in res_gemini.json()["prompt"]

    # 7. Request Revision on Task A from Claude Code
    res_rev = client.post("/api/tasks/task-api-A/request-revision", json={
        "feedback": "Missing CORS headers",
        "from_agent": "claude_code"
    })
    assert res_rev.status_code == 200
    assert res_rev.json()["status"] == "open"

    # Task A is now revision_requested
    task_a = client.get("/api/tasks/task-api-A").json()
    assert task_a["status"] == "revision_requested"

    # Task B is blocked again
    task_b = client.get("/api/tasks/task-api-B/wait-status").json()
    assert task_b["ready"] is False


def test_agent_heartbeat_and_nodes_registry(client):
    # Register Node 1: Codex on Alice's Laptop
    res1 = client.post("/api/agents/heartbeat", json={
        "instance_id": "codex@alice-laptop",
        "role": "codex",
        "hostname": "alice-macbook",
        "account": "alice",
        "status": "online"
    })
    assert res1.status_code == 200
    assert res1.json()["instance_id"] == "codex@alice-laptop"

    # Register Node 2: AntiGravity on Bob's Desktop
    res2 = client.post("/api/agents/heartbeat", json={
        "instance_id": "antigravity@bob-desktop",
        "role": "antigravity",
        "hostname": "bob-workstation",
        "account": "bob",
        "status": "online"
    })
    assert res2.status_code == 200

    # List all nodes
    res_list = client.get("/api/agents?nodes=true")
    assert res_list.status_code == 200
    agents = res_list.json()
    assert len(agents) >= 2
    ids = [a["instance_id"] for a in agents]
    assert "codex@alice-laptop" in ids
    assert "antigravity@bob-desktop" in ids

    # GET /api/agents returns loaded public adapters
    res_adapters = client.get("/api/agents")
    assert res_adapters.status_code == 200
    adapters = res_adapters.json()
    assert isinstance(adapters, list)
    assert any(a["id"] == "claude_code" for a in adapters)
    # Ensure command is NOT exposed in public adapters
    for a in adapters:
        assert "command" not in a

    # POST /api/agents/reload reloads adapters
    res_reload = client.post("/api/agents/reload")
    assert res_reload.status_code == 200
    assert res_reload.json()["status"] == "reloaded"


def test_token_authentication_middleware(tmp_path):
    db_file = str(tmp_path / "auth_test.db")
    app = create_app(db_path=db_file, auth_token="super-secret-key")
    auth_client = TestClient(app)

    # 1. Unauthenticated request to /api/jobs should return 401
    res_unauth = auth_client.get("/api/jobs")
    assert res_unauth.status_code == 401
    assert "Unauthorized" in res_unauth.json()["detail"]

    # 2. Invalid token should return 401
    res_bad = auth_client.get("/api/jobs", headers={"X-Agent-Relay-Token": "wrong-token"})
    assert res_bad.status_code == 401

    # 3. Valid token should return 200
    res_ok = auth_client.get("/api/jobs", headers={"X-Agent-Relay-Token": "super-secret-key"})
    assert res_ok.status_code == 200

    # 4. Bearer token format should also return 200
    res_bearer = auth_client.get("/api/jobs", headers={"Authorization": "Bearer super-secret-key"})
    assert res_bearer.status_code == 200


def test_mobile_pairing_reports_actual_port(tmp_path):
    """Regression test: /api/mobile/pairing must report the port the app was actually
    constructed with, not create_app's default of 8765 (the pairing QR code and deep
    link were previously unreachable whenever the hub was started on a non-default
    port, because the serve path lost the port before it reached create_app)."""
    db_file = str(tmp_path / "port_test.db")
    app = create_app(db_path=db_file, port=8845)
    client = TestClient(app, base_url="http://127.0.0.1:8845", client=("127.0.0.1", 50000))

    resp = client.get("/api/mobile/pairing")
    assert resp.status_code == 200
    data = resp.json()

    assert data["endpoints"]["localhost"].endswith(":8845")
    assert ":8845" in data["deep_link"]
    assert ":8765" not in data["deep_link"]
    for endpoint_url in data["endpoints"].values():
        assert ":8765" not in endpoint_url


def test_app_startup_binds_event_loop(tmp_path):
    """Verify FastAPI startup event triggers _bind_event_loop() and binds the running loop to engine."""
    db_file = str(tmp_path / "startup_test.db")
    app = create_app(db_path=db_file)
    engine = app.state.engine

    # Before startup context, loop is not bound
    assert engine._loop is None

    # TestClient entering its context manager triggers FastAPI startup event handlers
    with TestClient(app) as client:
        res = client.get("/api/jobs")
        assert res.status_code == 200
        # Startup event must have bound the active running event loop
        assert engine._loop is not None



def test_live_sessions_endpoint_is_empty_before_anything_is_dispatched(client):
    res = client.get("/api/console/live-sessions")
    assert res.status_code == 200
    assert res.json() == []


def test_live_sessions_endpoint_describes_and_orders_what_is_running(client):
    """The Sessions view needs enough to identify each process, busiest first."""
    import time as _time

    from agent_relay.core.live_sessions import LiveSession, live_session_key

    async def noop(*args):
        return None

    class _FakeStdin:
        def write(self, data):
            return None

        async def drain(self):
            return None

    class _FakeProcess:
        pid = 4242

        def __init__(self):
            self.stdin = _FakeStdin()
            self.stdout = None
            self.returncode = None

    runner = client.app.state.engine.runner
    for agent, cwd, idle in (("claude_code", "C:/one", 5.0), ("codex", "C:/two", 90.0)):
        session = LiveSession(
            agent=agent, cwd=cwd, dialect="claude", process=_FakeProcess(),
            on_delta=noop, on_complete=noop, on_process_lost=noop,
        )
        session.ui_session_id = f"sess-{agent}"
        session.last_activity = _time.monotonic() - idle
        runner.live_sessions[live_session_key(agent, cwd)] = session

    rows = client.get("/api/console/live-sessions").json()

    assert len(rows) == 2
    # Neither is busy, so the most recently active one leads.
    assert rows[0]["agent"] == "claude_code"
    assert rows[0]["working_directory"] == "C:/one"
    assert rows[0]["session_id"] == "sess-claude_code"
    assert rows[0]["busy"] is False
    assert rows[0]["pid"] == 4242
    assert rows[0]["idle_seconds"] < rows[1]["idle_seconds"]
    assert {"uptime_seconds", "turns_completed", "queued_turns"} <= set(rows[0])

    runner.live_sessions.clear()


def test_the_dashboard_page_is_never_cached(client):
    """A code or markup fix in index.html must reach the next page load.

    FileResponse sets no Cache-Control of its own, so a browser was free to
    keep serving this page from its own heuristic cache well after the hub
    had restarted with a fixed build, on a plain reload and even sometimes on
    a fresh navigation. The page every fix in this dashboard depends on being
    current must never be cached.
    """
    res = client.get("/")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-cache, no-store, must-revalidate"
