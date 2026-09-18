"""Tests for session and weekly usage telemetry.

These lock in the rule the Usage tab now follows: a figure appears only when
something real produced it. Every number the fetchers used to invent when a
provider said nothing is asserted absent here.
"""

import json

from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.db import Database
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage_fetcher import (
    _fetch_chatgpt_live,
    _fetch_claude_live,
    _fetch_gemini_live,
)


def _account(provider: str, **kwargs) -> UsageAccount:
    return UsageAccount(id=f"test-{provider}", provider=provider, name=provider, **kwargs)


def test_claude_reports_measured_tokens_and_no_invented_percentage(tmp_path, monkeypatch):
    """Claude Code usage is counted from its transcripts, with no percentage."""
    projects = tmp_path / "projects"
    session_dir = projects / "a-project"
    session_dir.mkdir(parents=True)
    record = {
        "type": "assistant",
        "timestamp": "2026-09-18T12:00:00.000Z",
        "requestId": "req-1",
        "message": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    session_dir.joinpath("session.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )

    import agent_relay.core.usage_fetcher as fetcher

    monkeypatch.setattr(fetcher, "read_claude_code_usage", lambda: _read(projects))

    acc = _fetch_claude_live(_account("claude"))

    assert acc.status == "active"
    assert acc.session_tokens_used == 15
    assert acc.weekly_tokens_used == 15
    assert "15 tokens" in (acc.session_reset_time or "")
    # No plan limit is published anywhere on this machine, so no share of one
    # may be shown.
    assert acc.session_percent_used is None
    assert acc.weekly_percent_used is None
    assert acc.percent_used is None
    assert acc.weekly_breakdown is None


def _read(projects):
    from datetime import datetime, timezone

    from agent_relay.core.claude_code_usage import read_usage

    return read_usage(
        projects_dir=projects,
        now=datetime(2026, 9, 18, 13, 0, tzinfo=timezone.utc),
    )


def test_claude_without_any_local_data_is_unavailable(tmp_path, monkeypatch):
    import agent_relay.core.usage_fetcher as fetcher

    monkeypatch.setattr(fetcher, "read_claude_code_usage", lambda: None)
    monkeypatch.setattr(fetcher, "_read_claude_plan", lambda account: False)

    acc = _fetch_claude_live(_account("claude"))

    assert acc.status == "unavailable"
    assert acc.error_message
    assert acc.session_percent_used is None
    assert acc.tokens_used is None


def test_chatgpt_without_a_sign_in_or_key_is_unavailable(tmp_path, monkeypatch):
    """No Codex sign-in and no API key means no usage, not a green 0%."""
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )

    acc = _fetch_chatgpt_live(_account("chatgpt"))

    assert acc.status == "unavailable"
    assert acc.session_percent_used is None
    assert acc.weekly_percent_used is None
    assert acc.error_message


def test_chatgpt_reports_the_windows_the_api_returns(tmp_path, monkeypatch):
    """The 5-hour and weekly figures are whatever ChatGPT says, unchanged."""
    codex = tmp_path / ".codex"
    codex.mkdir()
    codex.joinpath("auth.json").write_text(
        json.dumps({"tokens": {"access_token": "token-value", "account_id": "acct"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )

    body = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {"used_percent": 41.5, "reset_after_seconds": 7380},
            "secondary_window": {"used_percent": 12.0, "reset_after_seconds": 180000},
        },
    }
    _patch_httpx_get(monkeypatch, status_code=200, json_body=body)

    acc = _fetch_chatgpt_live(_account("chatgpt"))

    assert acc.status == "active"
    assert acc.session_percent_used == 41.5
    assert acc.session_reset_time == "Resets in 2h 3m"
    assert acc.weekly_percent_used == 12.0
    assert acc.weekly_reset_time == "Resets in 2d 2h"
    # ChatGPT reports no token count, so none is derived from the percentage.
    assert acc.tokens_used is None
    assert acc.tokens_limit is None


def test_chatgpt_refused_sign_in_is_unavailable(tmp_path, monkeypatch):
    codex = tmp_path / ".codex"
    codex.mkdir()
    codex.joinpath("auth.json").write_text(
        json.dumps({"tokens": {"access_token": "stale"}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )
    _patch_httpx_get(monkeypatch, status_code=401, json_body={})

    acc = _fetch_chatgpt_live(_account("chatgpt"))

    assert acc.status == "unavailable"
    assert acc.session_percent_used is None


def test_gemini_and_antigravity_report_unavailable_rather_than_constants(tmp_path, monkeypatch):
    """Neither tool publishes usage locally, so neither gets a number."""
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )

    acc = _fetch_gemini_live(_account("gemini", credential="ya29.some-oauth-token"))

    assert acc.status == "unavailable"
    assert acc.session_percent_used is None
    assert acc.session_percent_left is None
    assert acc.weekly_percent_used is None
    # The two invented model groups and the 9 / 52 pair are gone for good.
    assert acc.weekly_breakdown is None
    assert acc.error_message


def test_gemini_signed_in_account_is_named_in_the_reason(tmp_path, monkeypatch):
    gemini = tmp_path / ".gemini"
    gemini.mkdir()
    gemini.joinpath("google_accounts.json").write_text(
        json.dumps({"active": "someone@example.com"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )

    acc = _fetch_gemini_live(_account("gemini"))

    assert acc.status == "unavailable"
    assert "someone@example.com" in acc.error_message


def _patch_httpx_get(monkeypatch, status_code: int, json_body: dict):
    """Answer every outbound GET from the fetcher without touching the network."""

    class _Response:
        def __init__(self):
            self.status_code = status_code
            self.headers = {}

        def json(self):
            return json_body

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, *args, **kwargs):
            return _Response()

    monkeypatch.setattr("agent_relay.core.usage_fetcher.httpx.Client", _Client)


def test_database_telemetry_persistence(tmp_path):
    """Verify dual limit fields persist and load correctly from SQLite."""
    db_file = str(tmp_path / "test_telemetry.db")
    db = Database(db_file)

    acc = UsageAccount(
        id="acc-dual-limits",
        provider="claude",
        name="Personal Claude",
        session_title="Last 5 hours",
        session_reset_time="15 tokens over 1 turns",
        session_tokens_used=15,
        weekly_title="Last 7 days",
        weekly_reset_time="15 tokens over 1 turns",
        weekly_tokens_used=15,
    )
    db.save_usage_account(acc.model_dump())

    loaded = db.get_usage_account("acc-dual-limits")
    assert loaded is not None
    assert loaded["session_title"] == "Last 5 hours"
    assert loaded["session_tokens_used"] == 15
    assert loaded["weekly_tokens_used"] == 15
    assert loaded["session_percent_used"] is None


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
        "credential": "placeholder-token"
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
