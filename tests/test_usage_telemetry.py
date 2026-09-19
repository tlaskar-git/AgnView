"""Tests for session and weekly usage telemetry.

These lock in the rule the Usage tab now follows: a figure appears only when
something real produced it. Every number the fetchers used to invent when a
provider said nothing is asserted absent here.
"""

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.db import Database
from agent_relay.core.models import UsageAccount
from agent_relay.core.usage_fetcher import (
    _fetch_chatgpt_live,
    _fetch_claude_live,
    _fetch_gemini_live,
    usage_is_stale,
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
    # The sub-line says how the total was reached. It must not restate the
    # total itself, which the card already prints from session_tokens_used:
    # doing both rendered the same figure twice, back to back.
    assert acc.session_reset_time == "1 turn across 1 project, subagents included"
    assert "token" not in (acc.session_reset_time or "")
    assert "token" not in (acc.weekly_reset_time or "")
    # The window covers every project on this machine, so the label says so
    # rather than implying it is one repository's usage.
    assert acc.session_title == "Last 5 hours, this machine"
    assert acc.weekly_title == "Last 7 days, this machine"
    # No plan limit is published anywhere on this machine, so no share of one
    # may be shown.
    assert acc.session_percent_used is None
    assert acc.weekly_percent_used is None
    assert acc.percent_used is None
    assert acc.weekly_breakdown is None


def _ago(**kwargs) -> str:
    return (datetime.now(timezone.utc) - timedelta(**kwargs)).isoformat()


def _claude_with_transcripts(tmp_path, monkeypatch):
    """Give the Claude fetcher one countable turn to read on this machine."""
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
    monkeypatch.setattr(fetcher, "_read_claude_plan", lambda account: False)


def test_a_synced_percentage_survives_the_next_refresh(tmp_path, monkeypatch):
    """A real percentage from claude.ai outlives a local recompute.

    The telemetry sync reads the share of each window off Anthropic's own usage
    page. The local recompute has no percentage to offer, and it used to blank
    the synced one and replace the card with a token count within minutes of the
    sync. While the synced window is still open, the synced figures stand.
    """
    _claude_with_transcripts(tmp_path, monkeypatch)

    acc = _fetch_claude_live(
        _account(
            "claude",
            session_title="Current session",
            session_reset_time="Resets in 3h 12m",
            session_percent_used=37.0,
            session_percent_left=63.0,
            percent_used=37.0,
            session_telemetry_synced_at=_ago(hours=1),
            weekly_title="Weekly limit",
            weekly_reset_time="Resets Sat 7:00 PM",
            weekly_percent_used=12.0,
            weekly_percent_left=88.0,
            weekly_telemetry_synced_at=_ago(days=2),
        )
    )

    assert acc.status == "active"
    assert acc.session_percent_used == 37.0
    assert acc.percent_used == 37.0
    assert acc.session_title == "Current session"
    assert acc.session_reset_time == "Resets in 3h 12m"
    assert acc.weekly_percent_used == 12.0
    assert acc.weekly_reset_time == "Resets Sat 7:00 PM"
    # The card shows one figure per window. A synced percentage is the figure,
    # so the local token count does not appear beside it.
    assert acc.session_tokens_used is None
    assert acc.weekly_tokens_used is None


def test_a_synced_percentage_gives_way_to_tokens_once_its_window_closes(tmp_path, monkeypatch):
    """Past 5 hours for the session and 7 days for the week, the sync is history."""
    _claude_with_transcripts(tmp_path, monkeypatch)

    acc = _fetch_claude_live(
        _account(
            "claude",
            session_title="Current session",
            session_reset_time="Resets in 3h 12m",
            session_percent_used=37.0,
            session_percent_left=63.0,
            percent_used=37.0,
            session_telemetry_synced_at=_ago(hours=6),
            weekly_percent_used=12.0,
            weekly_percent_left=88.0,
            weekly_telemetry_synced_at=_ago(days=8),
        )
    )

    assert acc.status == "active"
    # Nothing real backs those percentages any more, so none is shown.
    assert acc.session_percent_used is None
    assert acc.session_percent_left is None
    assert acc.percent_used is None
    assert acc.weekly_percent_used is None
    # The honest local count takes over.
    assert acc.session_tokens_used == 15
    assert acc.weekly_tokens_used == 15
    assert acc.session_title == "Last 5 hours, this machine"


def test_a_stale_row_is_corrected_by_a_plain_get(tmp_path, monkeypatch):
    """An old row is recomputed on read, with no Refresh click involved.

    The list endpoint used to recompute only when a row had no window titles,
    so an account computed once by an older version of the code was served
    unchanged for ever. This row carries the old duplicated sub-line, which no
    version of the code writes now.
    """
    _claude_with_transcripts(tmp_path, monkeypatch)

    db_file = str(tmp_path / "stale.db")
    db = Database(db_file)
    db.save_usage_account(
        UsageAccount(
            id="acc-stale",
            provider="claude",
            name="Personal Claude Code",
            session_title="Last 5 hours",
            session_reset_time="15 tokens over 1 turns",
            session_tokens_used=15,
            weekly_title="Last 7 days",
            weekly_reset_time="15 tokens over 1 turns",
            weekly_tokens_used=15,
            last_checked=_ago(hours=3),
        ).model_dump()
    )

    client = TestClient(create_app(db_path=db_file))
    listed = client.get("/api/usage/accounts").json()

    assert len(listed) == 1
    row = listed[0]
    assert row["session_title"] == "Last 5 hours, this machine"
    assert row["session_reset_time"] == "1 turn across 1 project, subagents included"
    assert "tokens over" not in row["weekly_reset_time"]


def test_staleness_thresholds_differ_by_what_a_refresh_costs():
    """A local file walk is cheap and is redone often. A network read is not."""
    now = datetime.now(timezone.utc)
    two_minutes_ago = (now - timedelta(minutes=2)).isoformat()

    claude_code = _account("claude", last_checked=two_minutes_ago)
    assert usage_is_stale(claude_code, now) is True

    # An Anthropic API key is read over the network, so it follows the longer
    # threshold even though the provider is still Claude.
    api_key_account = _account(
        "claude", credential="sk-ant-placeholder", last_checked=two_minutes_ago
    )
    assert usage_is_stale(api_key_account, now) is False
    assert usage_is_stale(_account("chatgpt", last_checked=two_minutes_ago), now) is False
    assert (
        usage_is_stale(
            _account("chatgpt", last_checked=(now - timedelta(minutes=20)).isoformat()), now
        )
        is True
    )


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


# ----------------- Antigravity per-group session breakdown -----------------

ANTIGRAVITY_PAYLOAD = {
    "provider": "gemini",
    "weekly_breakdown": [
        {
            "group": "Gemini Models",
            "weekly_title": "Weekly Limit Remaining",
            "weekly_percent_used": 0.0,
            "weekly_percent_left": 100.0,
        },
        {
            "group": "Claude and GPT models",
            "weekly_title": "Weekly Limit Remaining",
            "weekly_percent_used": 0.0,
            "weekly_percent_left": 100.0,
        },
    ],
    "session_breakdown": [
        {
            "group": "Gemini Models",
            "session_title": "Five Hour Limit Remaining",
            "session_percent_used": 9.0,
            "session_percent_left": 91.0,
        },
        {
            "group": "Claude and GPT models",
            "session_title": "Five Hour Limit Remaining",
            "session_percent_used": 15.0,
            "session_percent_left": 85.0,
        },
    ],
    "session_percent_used": 15.0,
    "weekly_percent_used": 0.0,
}


def test_session_breakdown_is_stored_and_served(tmp_path):
    """Antigravity's per-group five-hour rows survive the round trip."""
    app = create_app(db_path=str(tmp_path / "breakdown.db"))
    client = TestClient(app)

    client.post("/api/usage/accounts", json={
        "id": "acc-antigravity",
        "provider": "gemini",
        "name": "Antigravity",
        "auth_type": "api_key",
        "credential": "placeholder-token",
    })
    synced = client.post(
        "/api/usage/accounts/acc-antigravity/telemetry", json=ANTIGRAVITY_PAYLOAD
    ).json()

    assert [row["group"] for row in synced["session_breakdown"]] == [
        "Gemini Models",
        "Claude and GPT models",
    ]
    assert synced["session_breakdown"][0]["session_percent_used"] == 9.0
    assert synced["session_breakdown"][1]["session_percent_used"] == 15.0
    # And it is still there on a plain read, not only in the sync response.
    fetched = client.get("/api/usage/accounts/acc-antigravity").json()
    assert fetched["session_breakdown"] == ANTIGRAVITY_PAYLOAD["session_breakdown"]
    assert fetched["weekly_breakdown"] == ANTIGRAVITY_PAYLOAD["weekly_breakdown"]


def test_a_breakdown_alone_stamps_the_window_it_measured(tmp_path):
    """Per-group rows count as a measurement, so the sync is stamped."""
    app = create_app(db_path=str(tmp_path / "stamp.db"))
    client = TestClient(app)

    client.post("/api/usage/accounts", json={
        "id": "acc-rows-only",
        "provider": "gemini",
        "name": "Antigravity",
        "auth_type": "api_key",
        "credential": "placeholder-token",
    })
    rows_only = {
        "provider": "gemini",
        "weekly_breakdown": ANTIGRAVITY_PAYLOAD["weekly_breakdown"],
        "session_breakdown": ANTIGRAVITY_PAYLOAD["session_breakdown"],
    }
    synced = client.post(
        "/api/usage/accounts/acc-rows-only/telemetry", json=rows_only
    ).json()

    assert synced["session_telemetry_synced_at"] is not None
    assert synced["weekly_telemetry_synced_at"] is not None


def test_session_breakdown_is_preserved_while_its_window_is_open(tmp_path, monkeypatch):
    """A five-hour breakdown holds for five hours, the same as a percentage."""
    _claude_with_transcripts(tmp_path, monkeypatch)
    rows = [{"group": "Gemini Models", "session_percent_used": 9.0}]

    acc = _fetch_claude_live(
        _account(
            "claude",
            session_percent_used=9.0,
            session_breakdown=rows,
            session_telemetry_synced_at=_ago(hours=1),
        )
    )

    assert acc.session_breakdown == rows


def test_session_breakdown_ages_out_on_the_session_clock(tmp_path, monkeypatch):
    """Past five hours nothing backs those rows, so they go.

    They are kept apart from weekly_breakdown for exactly this reason: a
    five-hour figure must not ride the seven-day clock.
    """
    _claude_with_transcripts(tmp_path, monkeypatch)

    acc = _fetch_claude_live(
        _account(
            "claude",
            session_percent_used=9.0,
            session_breakdown=[{"group": "Gemini Models", "session_percent_used": 9.0}],
            session_telemetry_synced_at=_ago(hours=6),
            weekly_telemetry_synced_at=_ago(hours=1),
            weekly_percent_used=4.0,
        )
    )

    assert acc.session_breakdown is None


def test_unavailable_clears_a_stale_session_breakdown(tmp_path, monkeypatch):
    """An account that can measure nothing shows no per-group rows either."""
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )

    acc = _fetch_gemini_live(
        _account(
            "gemini",
            credential="ya29.some-oauth-token",
            session_breakdown=[{"group": "Gemini Models", "session_percent_used": 9.0}],
        )
    )

    assert acc.status == "unavailable"
    assert acc.session_breakdown is None
    assert acc.weekly_breakdown is None


# ----------------- The one-time sync call to action -----------------


def test_the_sync_prompt_is_on_for_a_provider_with_nothing_measured():
    """Claude and Gemini can only get a real percentage from a browser sync."""
    assert _account("gemini").needs_telemetry_sync is True
    assert _account("claude").needs_telemetry_sync is True


def test_the_sync_prompt_is_never_shown_to_chatgpt():
    """Codex reads a real API, so its card must not ask for a sync it does not need."""
    assert _account("chatgpt").needs_telemetry_sync is False
    assert _account("chatgpt", session_percent_used=None).needs_telemetry_sync is False
    assert _account("deepseek").needs_telemetry_sync is False


def test_the_sync_prompt_goes_away_once_a_percentage_exists():
    """Any real figure, top level or per group, in either window, clears it."""
    assert _account("gemini", session_percent_used=9.0).needs_telemetry_sync is False
    assert _account("claude", weekly_percent_left=88.0).needs_telemetry_sync is False
    assert (
        _account(
            "gemini", session_breakdown=[{"group": "Gemini Models", "session_percent_used": 9.0}]
        ).needs_telemetry_sync
        is False
    )
    assert (
        _account(
            "gemini", weekly_breakdown=[{"group": "Gemini Models", "weekly_percent_used": 0.0}]
        ).needs_telemetry_sync
        is False
    )
    # A token count is not a percentage, so the prompt stays.
    assert _account("claude", session_tokens_used=15).needs_telemetry_sync is True
    # Nor is a row that carries only a label.
    assert (
        _account("gemini", weekly_breakdown=[{"group": "Gemini Models"}]).needs_telemetry_sync
        is True
    )


def test_the_api_serves_the_sync_prompt_flag(tmp_path):
    """The Usage tab reads this field, so the API must send it."""
    app = create_app(db_path=str(tmp_path / "cta.db"))
    client = TestClient(app)

    client.post("/api/usage/accounts", json={
        "id": "acc-cta",
        "provider": "gemini",
        "name": "Antigravity",
        "auth_type": "api_key",
        "credential": "placeholder-token",
    })
    assert client.get("/api/usage/accounts/acc-cta").json()["needs_telemetry_sync"] is True

    client.post("/api/usage/accounts/acc-cta/telemetry", json=ANTIGRAVITY_PAYLOAD)
    assert client.get("/api/usage/accounts/acc-cta").json()["needs_telemetry_sync"] is False


def test_an_antigravity_sync_survives_the_next_automatic_refresh(tmp_path, monkeypatch):
    """The refresh has nothing to put in its place, so it leaves the sync alone.

    Without this the Gemini fetcher blanked every window on the next refresh,
    and the one-time sync the operator had just done was gone within minutes.
    """
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )

    acc = _fetch_gemini_live(
        _account(
            "gemini",
            session_percent_used=15.0,
            session_breakdown=ANTIGRAVITY_PAYLOAD["session_breakdown"],
            session_telemetry_synced_at=_ago(hours=1),
            weekly_percent_used=0.0,
            weekly_breakdown=ANTIGRAVITY_PAYLOAD["weekly_breakdown"],
            weekly_telemetry_synced_at=_ago(days=2),
        )
    )

    assert acc.status == "active"
    assert acc.session_percent_used == 15.0
    assert acc.session_breakdown == ANTIGRAVITY_PAYLOAD["session_breakdown"]
    assert acc.weekly_breakdown == ANTIGRAVITY_PAYLOAD["weekly_breakdown"]
    assert acc.needs_telemetry_sync is False


def test_an_expired_antigravity_sync_goes_back_to_unavailable(tmp_path, monkeypatch):
    """Past its window the sync is history, and the card asks for a new one."""
    monkeypatch.setattr(
        "agent_relay.core.usage_fetcher.Path.home", staticmethod(lambda: tmp_path)
    )

    acc = _fetch_gemini_live(
        _account(
            "gemini",
            session_percent_used=15.0,
            session_breakdown=ANTIGRAVITY_PAYLOAD["session_breakdown"],
            session_telemetry_synced_at=_ago(hours=6),
            weekly_percent_used=0.0,
            weekly_breakdown=ANTIGRAVITY_PAYLOAD["weekly_breakdown"],
            weekly_telemetry_synced_at=_ago(days=8),
        )
    )

    assert acc.status == "unavailable"
    assert acc.session_breakdown is None
    assert acc.weekly_breakdown is None
    assert acc.needs_telemetry_sync is True
