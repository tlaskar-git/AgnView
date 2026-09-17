"b""Tests for exact session and weekly subscription usage limit telemetry."""

from fastapi.testclient import TestClient
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage_fetcher import (
    _fetch_claude_live,
    _fetch_chatgpt_live,
    _fetch_gemini_live,
)
from agent_relay.core.db import Database
from agent_relay.api.app import create_app


def test_claude_telemetry_fields():
    """Verify Claude account populates Current session and Weekly limits matching claude.ai settings."""
    acc = UsageAccount(
        id="test-claude",
        provider="claude",
        name="Claude Max",
        credential="claude-cli-test",
        auth_type="cli"
    )
    acc = _fetch_claude_live(acc)

    assert acc.plan_label == "Max (20x)"
    assert acc.session_title == "Current session"
    assert "Resets in" in (acc.session_reset_time or "")
    assert acc.session_percent_used is not None
    assert acc.session_percent_used >= 0.0
    assert acc.weekly_title == "Weekly limits"
    assert "Resets Sat" in (acc.weekly_reset_time or "")
    assert acc.weekly_breakdown is not None
    assert len(acc.weekly_breakdown) >= 2
    assert acc.weekly_breakdown[0]["label"] == "All models"
    assert acc.weekly_breakdown[1]["label"] == "Fable"


def test_chatgpt_telemetry_fields():
    """Verify ChatGPT account populates 5-hour limit and Weekly limit matching chatgpt.com settings."""
    acc = UsageAccount(
        id="test-chatgpt",
        provider="chatgpt",
        name="ChatGPT Plus",
        credential="mock-jwt-token"
    )
    acc = _fetch_chatgpt_live(acc)

    assert "Plus" in (acc.plan_label or "")
    assert acc.session_title == "5-hour limit"
    assert "Resets in" in (acc.session_reset_time or "")
    assert acc.session_percent_left is not None
    assert acc.weekly_title == "Weekly limit"
    assert "Resets in" in (acc.weekly_reset_time or "")
    assert acc.weekly_percent_left is not None


def test_gemini_telemetry_fields():
    """Verify Gemini account populates Five Hour Limit Remaining and Weekly Limit Remaining matching Google Antigravity / Gemini desktop app."""
    acc = UsageAccount(
        id="test-gemini",
        provider="gemini",
        name="Gemini Pro",
        credential="ya29.mock-test-token"
    )
    acc = _fetch_gemini_live(acc)

    assert acc.plan_label == "PRO"
    assert "Five Hour Limit" in acc.session_title
    assert "Limit Remaining" in acc.session_title
    assert acc.session_percent_left == 9.0
    assert "Weekly Limit" in acc.weekly_title
    assert "Limit Remaining" in acc.weekly_title
    assert acc.weekly_percent_left == 52.0
    assert acc.weekly_breakdown is not None
    assert len(acc.weekly_breakdown) == 2
    assert acc.weekly_breakdown[0]["group"] == "Gemini Models"
    assert acc.weekly_breakdown[1]["group"] == "Claude and GPT models"


def test_database_telemetry_persistence(tmp_path):
    """Verify dual limit fields persist and load correctly from SQLite."""
    db_file = str(tmp_path / "test_telemetry.db")
    db = Database(db_file)

    acc = UsageAccount(
        id="acc-dual-limits",
        provider="claude",
        name="Personal Claude",
        session_title="Current session",
        session_reset_time="Resets in 1 hr 34 min",
        session_percent_used=4.0,
        weekly_title="Weekly limits",
        weekly_reset_time="Resets Sat 7:00 PM",
        weekly_percent_used=0.0,
        weekly_breakdown=[
            {"label": "All models", "percent_used": 0.0, "reset_time": "Resets Sat 7:00 PM"},
            {"label": "Fable", "percent_used": 0.0, "reset_time": "Resets Sat 7:00 PM"}
        ]
    )
    db.save_usage_account(acc.model_dump())

    loaded = db.get_usage_account("acc-dual-limits")
    assert loaded is not None
    assert loaded["session_title"] == "Current session"
    assert loaded["session_reset_time"] == "Resets in 1 hr 34 min"
    assert loaded["session_percent_used"] == 4.0
    assert loaded["weekly_title"] == "Weekly limits"
    assert len(loaded["weekly_breakdown"]) == 2


def test_telemetry_api_sync(tmp_path):
    """Verify POST /api/usage/accounts/{id}/telemetry updates dual-limit fields."""
    db_file = str(tmp_path / "test_api.db")
    app = create_app(db_path=db_file)
    client = TestClient(app)

    create_res = client.post("/api/usage/accounts", json={
        "id": "acc-sync-test",
        "provider": "gemini",
        "name": "Sync Test",
        "auth_type": "api_key",
        "credential": "mock-token"
    })
    assert create_res.status_code == 200

    telemetry_payload = {
        "plan_label": "PRO",
        "session_title": "Current usage",
        "session_reset_time": "Resets at 23:16",
        "session_percent_used": 0.0,
        "weekly_title": "Weekly limit",
        "weekly_reset_time": "Resets on 16 Sept at 13:16",
        "weekly_percent_used": 3.0
    }
    sync_res = client.post("/api/usage/accounts/acc-sync-test/telemetry", json=telemetry_payload)
    assert sync_res.status_code == 200
    synced = sync_res.json()
    assert synced["session_title"] == "Current usage"
    assert synced["session_reset_time"] == "Resets at 23:16"
    assert synced["session_percent_used"] == 0.0
    assert synced["weekly_title"] == "Weekly limit"
    assert synced["weekly_percent_used"] == 3.0
