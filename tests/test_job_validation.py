"""Job creation must reject malformed specifications.

An empty title, a job with no tasks, and a task assigned to an agent this
build does not coordinate all used to return 200 and create a job.
"""

import os
import tempfile

from fastapi.testclient import TestClient

from agent_relay.api.app import create_app


def _client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return TestClient(create_app(db_path=path))


def _valid_task(**over):
    task = {"id": "t1", "title": "Do the thing", "assigned_agent": "codex"}
    task.update(over)
    return task


def test_empty_title_is_rejected():
    client = _client()
    res = client.post("/api/jobs", json={"title": "   ", "tasks": [_valid_task()]})
    assert res.status_code == 400
    assert "title" in res.json()["detail"].lower()


def test_zero_tasks_is_rejected():
    client = _client()
    res = client.post("/api/jobs", json={"title": "Real job", "tasks": []})
    assert res.status_code == 400
    assert "at least one task" in res.json()["detail"].lower()


def test_unknown_assigned_agent_is_rejected():
    client = _client()
    res = client.post("/api/jobs", json={
        "title": "Real job",
        "tasks": [_valid_task(assigned_agent="skynet")],
    })
    assert res.status_code == 400
    assert "unknown agent" in res.json()["detail"].lower()


def test_task_without_a_title_is_rejected():
    client = _client()
    res = client.post("/api/jobs", json={
        "title": "Real job",
        "tasks": [_valid_task(title="")],
    })
    assert res.status_code == 400


def test_known_agents_and_instance_assignments_are_accepted():
    client = _client()
    tasks = [
        {"id": "a", "title": "A", "assigned_agent": "claude_code"},
        {"id": "b", "title": "B", "assigned_agent": "codex"},
        {"id": "c", "title": "C", "assigned_agent": "antigravity"},
        {"id": "e", "title": "E", "assigned_agent": "gemini"},
        # A specific instance of a known role.
        {"id": "f", "title": "F", "assigned_agent": "codex@alice-laptop"},
    ]
    res = client.post("/api/jobs", json={"title": "Every agent", "tasks": tasks})
    assert res.status_code == 200
    assert len(res.json()["tasks"]) == 5


def test_a_valid_job_is_still_created():
    client = _client()
    res = client.post("/api/jobs", json={"title": "Real job", "tasks": [_valid_task()]})
    assert res.status_code == 200
    assert res.json()["title"] == "Real job"
