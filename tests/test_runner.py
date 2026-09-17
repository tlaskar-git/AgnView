"""Tests for AgentRunner and Interactive Console endpoints."""

import pytest
from fastapi.testclient import TestClient
from agent_relay.api.app import create_app
from agent_relay.core.db import Database
from agent_relay.core.runner import AgentRunner


@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "runner_test.db")
    app = create_app(db_path=db_file)
    return TestClient(app)


@pytest.mark.anyio
async def test_agent_runner_emit_and_retrieve(tmp_path):
    db_file = str(tmp_path / "unit_runner.db")
    db = Database(db_file)
    runner = AgentRunner(db)

    # Dispatch a test instruction to a synthetic agent
    await runner.dispatch(
        agent="antigravity",
        prompt="Verify database schema and prepare migration",
        cwd=str(tmp_path),
        session_id="test-sess-1"
    )

    # Check that console logs were recorded
    logs = db.get_console_logs(session_id="test-sess-1")
    assert len(logs) >= 2  # user prompt + agent output / finished notice

    # Verify user log entry
    user_entry = next((log for log in logs if log["source"] == "user_input"), None)
    assert user_entry is not None
    assert "Verify database schema" in user_entry["content"]

    # Verify agent log entry
    agent_entry = next((log for log in logs if log["agent"] == "antigravity"), None)
    assert agent_entry is not None


def test_console_api_endpoints(client):
    # 1. Dispatch command
    res_dispatch = client.post("/api/console/dispatch", json={
        "agent": "codex",
        "prompt": "Synthesize FastAPI endpoints",
        "session_id": "api-sess-42"
    })
    assert res_dispatch.status_code == 200
    dispatch_data = res_dispatch.json()
    assert dispatch_data["status"] == "dispatched"
    assert dispatch_data["agent"] == "codex"
    assert dispatch_data["session_id"] == "api-sess-42"

    # 2. Get console logs
    res_logs = client.get("/api/console/logs")
    assert res_logs.status_code == 200
    logs = res_logs.json()
    assert isinstance(logs, list)

    # 3. Clear console logs
    res_clear = client.post("/api/console/clear")
    assert res_clear.status_code == 200
    assert res_clear.json()["status"] == "cleared"

    # Verify logs are now empty
    res_logs_after = client.get("/api/console/logs")
    assert res_logs_after.status_code == 200
    assert len(res_logs_after.json()) == 0
