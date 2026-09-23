"""A card keeps its last real reading while the plan windows are open, and a
Claude card asks Claude Code to renew an expiring sign-in."""

import json
import time
from datetime import timedelta

from agent_relay.core.usage import models
from agent_relay.core.usage.adapters import claude
from agent_relay.core.usage.models import (
    CONFIDENCE_DERIVED,
    CONFIDENCE_MEASURED,
    UNIT_PERCENT,
    UNIT_TOKENS,
    UsageObservation,
    UsageWindow,
    utc_now,
)
from agent_relay.core.usage.service import (
    CHECKED_NOTE,
    HELD_NOTE,
    hold_last_reading,
    observation_is_stale,
)


def _measured(minutes_ago=10, session_ends_in=60, week_ends_in=3000):
    now = utc_now()
    return UsageObservation(
        source="claude_oauth_api",
        confidence=CONFIDENCE_MEASURED,
        measured_at=now - timedelta(minutes=minutes_ago),
        expected_refresh_seconds=300,
        windows=[
            UsageWindow(key="session", label="Session", unit=UNIT_PERCENT, used=40.0, limit=100.0,
                        window_end=now + timedelta(minutes=session_ends_in)),
            UsageWindow(key="week", label="Weekly", unit=UNIT_PERCENT, used=12.0, limit=100.0,
                        window_end=now + timedelta(minutes=week_ends_in)),
        ],
    )


def _derived():
    return UsageObservation(
        source="claude_code_transcripts",
        confidence=CONFIDENCE_DERIVED,
        windows=[UsageWindow(key="session", label="Tokens", unit=UNIT_TOKENS, used=1000.0)],
        expected_refresh_seconds=60,
    )


def test_a_failed_read_keeps_the_last_real_reading_with_its_age():
    previous = _measured(minutes_ago=10)
    new = UsageObservation.unavailable("claude_oauth_api", "The sign-in has expired.", expected_refresh_seconds=300)

    held = hold_last_reading(new, previous)

    assert held.is_measured
    assert held.measured_at == previous.measured_at
    assert [w.used for w in held.windows] == [40.0, 12.0]
    assert held.notes[HELD_NOTE] is True
    assert "The sign-in has expired." in held.error
    assert held.error.startswith("Showing the last real reading")


def test_a_weaker_source_does_not_replace_a_real_reading():
    held = hold_last_reading(_derived(), _measured())
    assert held.is_measured
    assert held.notes["latest_source"] == "claude_code_transcripts"


def test_a_new_real_reading_always_wins():
    fresh = _measured(minutes_ago=0)
    fresh.windows[0].used = 55.0
    assert hold_last_reading(fresh, _measured()) is fresh


def test_windows_that_reset_are_dropped_from_a_held_reading():
    previous = _measured(session_ends_in=-5, week_ends_in=3000)
    held = hold_last_reading(_derived(), previous)
    assert [w.key for w in held.windows] == ["week"]


def test_nothing_is_held_once_every_window_has_reset():
    new = _derived()
    assert hold_last_reading(new, _measured(session_ends_in=-5, week_ends_in=-1)) is new


def test_a_held_reading_retries_on_the_last_attempt_not_its_age():
    held = hold_last_reading(_derived(), _measured(minutes_ago=120))
    now = utc_now()
    held.expected_refresh_seconds = 300
    assert not observation_is_stale(held, now)
    held.notes[CHECKED_NOTE] = (now - timedelta(seconds=301)).isoformat()
    assert observation_is_stale(held, now)


def _write_credentials(home, expires_in_seconds):
    path = home / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "test-access",
        "expiresAt": int((time.time() + expires_in_seconds) * 1000),
        "subscriptionType": "max",
    }}))
    return path


def _reset_refresh_state(monkeypatch):
    monkeypatch.setattr(claude, "_last_refresh_attempt", 0.0)
    monkeypatch.setattr(claude, "_last_refresh_error", None)


def test_an_expired_sign_in_is_renewed_by_claude_code(tmp_path, monkeypatch):
    path = _write_credentials(tmp_path, -60)
    monkeypatch.setattr(claude, "CREDENTIALS_PATH", path)
    monkeypatch.setattr(claude.platform, "system", lambda: "Linux")
    monkeypatch.setenv("AGNVIEW_CLAUDE_REFRESH", "1")
    monkeypatch.setattr(claude, "_claude_command", lambda: "claude")
    _reset_refresh_state(monkeypatch)
    calls = []

    class Done:
        returncode = 0
        stdout = "OK"
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        _write_credentials(tmp_path, 8 * 3600)
        return Done()

    monkeypatch.setattr(claude.subprocess, "run", fake_run)

    oauth, problem = claude.fresh_claude_oauth()

    assert problem is None
    assert calls and calls[0][:2] == ["claude", "-p"]
    assert "--model" in calls[0] and "haiku" in calls[0]
    assert claude._seconds_left(oauth) > 3600


def test_refresh_attempts_are_rate_limited(tmp_path, monkeypatch):
    path = _write_credentials(tmp_path, -60)
    monkeypatch.setattr(claude, "CREDENTIALS_PATH", path)
    monkeypatch.setattr(claude.platform, "system", lambda: "Linux")
    monkeypatch.setenv("AGNVIEW_CLAUDE_REFRESH", "1")
    monkeypatch.setattr(claude, "_claude_command", lambda: "claude")
    _reset_refresh_state(monkeypatch)
    calls = []

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Not logged in"

    def fake_run(args, **kwargs):
        calls.append(args)
        return Failed()

    monkeypatch.setattr(claude.subprocess, "run", fake_run)

    _, first = claude.fresh_claude_oauth()
    _, second = claude.fresh_claude_oauth()

    assert len(calls) == 1
    assert "Not logged in" in first
    assert second == first


def test_refresh_can_be_switched_off(tmp_path, monkeypatch):
    path = _write_credentials(tmp_path, -60)
    monkeypatch.setattr(claude, "CREDENTIALS_PATH", path)
    monkeypatch.setattr(claude.platform, "system", lambda: "Linux")
    monkeypatch.setenv("AGNVIEW_CLAUDE_REFRESH", "0")
    _reset_refresh_state(monkeypatch)
    monkeypatch.setattr(claude.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))

    _, problem = claude.fresh_claude_oauth()
    assert "off" in problem


def test_models_module_is_the_one_the_service_uses():
    # Guards the imports above against a silent rename.
    assert models.CONFIDENCE_MEASURED == CONFIDENCE_MEASURED
