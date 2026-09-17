"""Unit tests for notifications manager and endpoints."""

import pytest
import asyncio
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
import httpx

from agent_relay.core.notifications import NotificationManager, NotificationResult
from agent_relay.api.app import create_app
from agent_relay.core.models import JobStatus, CreateJobRequest, TaskSpec


def test_notifications_manager_init_and_public_channels(tmp_path):
    config_file = tmp_path / "notifications.yaml"
    yaml_content = """
channels:
  ntfy:
    topic: test-alerts
    events:
      - pipeline_failed
  discord:
    webhook_url: https://discord.com/api/webhooks/123/secret_token_abc
    events:
      - pipeline_complete
"""
    config_file.write_text(yaml_content, encoding="utf-8")
    manager = NotificationManager(config_path=config_file)
    public = manager.get_public_channels()
    assert len(public) == 2
    discord_channel = next(c for c in public if c["name"] == "discord")
    # Secret must be masked
    assert "secret_token_abc" not in discord_channel["config"]["webhook_url"]
    assert "..." in discord_channel["config"]["webhook_url"]


def test_notify_event_dispatch(tmp_path):
    config_file = tmp_path / "notifications.yaml"
    yaml_content = """
channels:
  raw:
    url: https://example.com/webhook
    events:
      - pipeline_failed
"""
    config_file.write_text(yaml_content, encoding="utf-8")
    manager = NotificationManager(config_path=config_file)

    with patch("httpx.AsyncClient.request", new_callable=AsyncMock) as mock_req:
        mock_resp = httpx.Response(status_code=200, request=httpx.Request("POST", "https://example.com/webhook"))
        mock_req.return_value = mock_resp

        results = asyncio.run(manager.notify_event("pipeline_failed", {
            "pipeline_id": "job-1",
            "pipeline_title": "Build Test",
            "status": "failed",
            "failed_task": "task-test",
            "duration": "10s"
        }))

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].channel == "raw"
        assert mock_req.called


def test_test_channel(tmp_path):
    config_file = tmp_path / "notifications.yaml"
    yaml_content = """
channels:
  ntfy:
    topic: my-topic
    events:
      - pipeline_complete
"""
    config_file.write_text(yaml_content, encoding="utf-8")
    manager = NotificationManager(config_path=config_file)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_resp = httpx.Response(status_code=200, request=httpx.Request("POST", "https://ntfy.sh/my-topic"))
        mock_post.return_value = mock_resp

        res = asyncio.run(manager.test_channel("ntfy"))
        assert res.success is True
        assert res.channel == "ntfy"
        assert mock_post.called


def test_notifications_api_endpoints(tmp_path):
    db_file = str(tmp_path / "test_notif.db")
    app = create_app(db_path=db_file)
    client = TestClient(app)

    # 1. GET /api/notifications returns channels list
    res = client.get("/api/notifications")
    assert res.status_code == 200
    assert "channels" in res.json()

    # 2. POST /api/notifications/test succeeds
    with patch("agent_relay.core.notifications.NotificationManager.test_channel", new_callable=AsyncMock) as mock_test:
        mock_test.return_value = NotificationResult(channel="ntfy", success=True, status_code=200)
        res_test = client.post("/api/notifications/test?channel=ntfy")
        assert res_test.status_code == 200
        assert res_test.json()["channel"] == "ntfy"
        assert res_test.json()["success"] is True


def test_engine_fail_task_and_complete_task_notifications(tmp_path):
    db_file = str(tmp_path / "test_engine_notif.db")
    app = create_app(db_path=db_file)
    engine = app.state.engine

    # Create job with 1 task
    req = CreateJobRequest(
        id="job-notif-1",
        title="Notification Job",
        description="Testing notification triggers",
        tasks=[TaskSpec(id="t1", title="First Task", assigned_agent="claude_code", dependencies=[])]
    )
    job = engine.create_job(req)

    # Test completing task
    with patch.object(engine.notification_manager, "notify_event", new_callable=AsyncMock):
        engine.claim_task("t1", agent="claude_code")
        engine.complete_task("t1", summary="Finished perfectly")
        # Check job completion triggered
        assert engine.get_job(job.id).status == JobStatus.COMPLETED

    # Test failing a task
    req2 = CreateJobRequest(
        id="job-notif-2",
        title="Failing Job",
        description="Testing fail triggers",
        tasks=[TaskSpec(id="t2", title="Faulty Task", assigned_agent="claude_code", dependencies=[])]
    )
    job2 = engine.create_job(req2)
    with patch.object(engine.notification_manager, "notify_event", new_callable=AsyncMock):
        engine.claim_task("t2", agent="claude_code")
        engine.fail_task("t2", reason="Fatal error occurred")
        assert engine.get_job(job2.id).status == JobStatus.FAILED



def _raw_manager(tmp_path):
    config_file = tmp_path / "notifications.yaml"
    config_file.write_text(
        "channels:\n"
        "  raw:\n"
        "    url: https://example.invalid/hook\n"
        "    events: [\"all\"]\n",
        encoding="utf-8",
    )
    return NotificationManager(config_path=config_file)


def test_delivery_timeout_is_five_seconds():
    from agent_relay.core.notifications import DELIVERY_TIMEOUT
    assert DELIVERY_TIMEOUT == 5.0


def test_retry_backoff_is_one_two_four(tmp_path):
    """Failed delivery retries 3 times with 1s/2s/4s backoff."""
    manager = _raw_manager(tmp_path)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
         patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_post.side_effect = httpx.ConnectError("unreachable")
        res = asyncio.run(manager.notify_event("pipeline_failed", {"pipeline_id": "job-1"}))

    assert len(res) == 1
    assert res[0].success is False
    assert res[0].attempts == 4  # 1 initial + 3 retries
    assert mock_post.call_count == 4
    assert [c.args[0] for c in mock_sleep.call_args_list] == [1.0, 2.0, 4.0]


def test_no_retry_on_success(tmp_path):
    manager = _raw_manager(tmp_path)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "https://example.invalid/hook")
        )
        res = asyncio.run(manager.notify_event("pipeline_complete", {"pipeline_id": "job-1"}))
    assert res[0].success is True
    assert res[0].attempts == 1
    assert mock_post.call_count == 1


def test_config_error_is_not_retried(tmp_path):
    config_file = tmp_path / "notifications.yaml"
    config_file.write_text(
        "channels:\n  discord:\n    webhook_url: \"\"\n    events: [\"all\"]\n",
        encoding="utf-8",
    )
    manager = NotificationManager(config_path=config_file)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        res = asyncio.run(manager.notify_event("pipeline_failed", {"pipeline_id": "job-1"}))
    assert res[0].success is False
    assert mock_post.call_count == 0


@pytest.mark.parametrize("event", [
    "task_completed",
    "task_failed",
    "agent_finished",
    "revision_requested",
    "job_completed",
    "pipeline_complete",
    "pipeline_failed",
])
def test_all_trigger_events_dispatch(tmp_path, event):
    """Every documented trigger event produces a dispatch with a real title."""
    manager = _raw_manager(tmp_path)
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = httpx.Response(
            200, request=httpx.Request("POST", "https://example.invalid/hook")
        )
        res = asyncio.run(manager.notify_event(event, {
            "pipeline_id": "job-1", "pipeline_title": "Demo",
            "task_id": "t1", "agent": "codex",
        }))
    assert mock_post.call_count == 1
    assert res[0].success is True
    body = mock_post.call_args.kwargs["json"]
    assert body["event"] == event
    assert "AgnView Alert" not in body["title"]  # has a purpose-built title


def test_delivery_failure_surfaces_system_notice(tmp_path):
    """Exhausted delivery emits a system_notice into the console stream."""
    from agent_relay.core.db import Database
    from agent_relay.core.engine import RelayEngine

    engine = RelayEngine(db=Database(db_path=str(tmp_path / "t.db")))
    engine.notification_manager = _raw_manager(tmp_path)
    q = engine.subscribe_events()

    async def run():
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            mock_post.side_effect = httpx.ConnectError("unreachable")
            await engine.dispatch_notification(
                "task_failed", {"pipeline_id": "job-1"}, job_id="job-1"
            )

    asyncio.run(run())

    notices = []
    while not q.empty():
        evt = q.get_nowait()
        if evt["payload"].get("source") == "system_notice":
            notices.append(evt["payload"]["content"])

    assert len(notices) == 1
    assert "raw" in notices[0]
    assert "task_failed" in notices[0]
    assert "unreachable" in notices[0]
