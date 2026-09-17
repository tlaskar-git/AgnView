"""Tests for the task failure transition.

The 'failed' and 'blocked' statuses were documented but unreachable: no API
route or engine call could produce either.
"""

import os
import tempfile

from fastapi.testclient import TestClient

from agent_relay.api.app import create_app


def _client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return TestClient(create_app(db_path=path))


def _job(client):
    return client.post("/api/jobs", json={
        "id": "fail-job",
        "title": "Failure job",
        "tasks": [
            {"id": "a", "title": "A", "assigned_agent": "codex"},
            {"id": "b", "title": "B", "assigned_agent": "claude_code", "dependencies": ["a"]},
            {"id": "c", "title": "C", "assigned_agent": "antigravity", "dependencies": ["b"]},
        ],
    })


def test_failing_a_task_blocks_downstream_and_fails_the_job():
    client = _client()
    assert _job(client).status_code == 200

    res = client.post("/api/tasks/a/fail", json={"reason": "build exited 1", "agent": "codex"})
    assert res.status_code == 200
    assert res.json()["status"] == "failed"
    assert res.json()["output_summary"] == "build exited 1"

    job = client.get("/api/jobs/fail-job").json()
    assert job["status"] == "failed"
    # Both direct and transitive dependents are blocked.
    assert job["tasks"]["b"]["status"] == "blocked"
    assert job["tasks"]["c"]["status"] == "blocked"


def test_failure_reason_is_required():
    client = _client()
    _job(client)
    assert client.post("/api/tasks/a/fail", json={"reason": "   "}).status_code == 422


def test_cannot_fail_a_completed_task():
    client = _client()
    _job(client)
    client.post("/api/tasks/a/claim", json={"agent": "codex"})
    client.post("/api/tasks/a/complete", json={"summary": "done"})
    res = client.post("/api/tasks/a/fail", json={"reason": "too late"})
    assert res.status_code == 409


def test_failing_an_unknown_task_is_404():
    client = _client()
    _job(client)
    assert client.post("/api/tasks/nope/fail", json={"reason": "x"}).status_code == 404
