"""Regression tests for the usage rebuild.

Each test here corresponds to one of the defects recorded in
docs/adr/ADR-USAGE-OBSERVATIONS.md. They are written against the behaviour that
was wrong, not against the implementation, so a future refactor that brings the
defect back still fails them.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_relay.core.models import UsageAccount, UsageTelemetryPayload
from agent_relay.core.usage import (
    CONFIDENCE_DERIVED,
    CONFIDENCE_MEASURED,
    CONFIDENCE_UNAVAILABLE,
    UNIT_CURRENCY,
    UNIT_PERCENT,
    UNIT_TOKENS,
    PROVIDERS,
    PlanInfo,
    UnknownProvider,
    UsageObservation,
    UsageWindow,
    format_countdown,
    get_adapter,
    is_known,
    provider_options,
    render_observation,
)
from agent_relay.core.usage.snippets import build_sync_snippet

REPO_ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = REPO_ROOT / "agent_relay" / "web" / "templates" / "index.html"


def _observation(measured_at, window_end, percent=40.0):
    return UsageObservation(
        source="claude_oauth_api",
        measured_at=measured_at,
        confidence=CONFIDENCE_MEASURED,
        expected_refresh_seconds=300,
        windows=[
            UsageWindow(
                key="session",
                label="Session",
                unit=UNIT_PERCENT,
                used=percent,
                limit=100.0,
                window_end=window_end,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Defect 1 and 2: a stored countdown froze, and a stale figure posed as current
# ---------------------------------------------------------------------------


def test_countdown_is_recomputed_as_the_clock_advances():
    """The same observation must report less time left an hour later.

    The old design stored "Resets in 6d 22h" as text. It was true when written
    and wrong for ever after, and no instant was kept to recompute it from.
    """
    start = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    window_end = start + timedelta(hours=5)
    observation = _observation(start, window_end)

    at_start = render_observation(observation, now=start)["windows"][0]["countdown_text"]
    later = render_observation(observation, now=start + timedelta(hours=3))["windows"][0][
        "countdown_text"
    ]

    assert at_start == "Resets in 5h 0m"
    assert later == "Resets in 2h 0m"
    assert at_start != later


def test_a_three_day_old_reading_is_marked_stale():
    """Age is always rendered, and an old figure is flagged rather than hidden.

    This is the exact case that was on the dashboard: a percentage synced on
    19 September shown as current on 22 September.
    """
    measured = datetime(2026, 9, 19, 19, 41, tzinfo=timezone.utc)
    now = datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc)
    view = render_observation(_observation(measured, now + timedelta(days=4)), now=now)

    assert view["is_stale"] is True
    assert view["age_text"] == "measured 2d 21h ago"
    assert view["source_label"] == "Anthropic account usage"


def test_a_closed_window_says_so_rather_than_counting_backwards():
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    assert format_countdown(now - timedelta(minutes=5), now) == "Window closed, awaiting refresh"


def test_no_countdown_when_the_source_gave_no_end_instant():
    """A missing instant renders nothing, never a guess."""
    now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    assert format_countdown(None, now) is None


def test_a_fresh_read_replaces_rather_than_merges():
    """Nothing carries a previous figure forward into a new observation.

    The old code captured a synced percentage and restored it over every
    recompute, also re-marking the account active and clearing its error. An
    account holds exactly one observation now, so there is nothing to merge.
    """
    account = UsageAccount(provider="claude", name="test")
    account.observation = _observation(
        datetime(2026, 9, 19, tzinfo=timezone.utc),
        datetime(2026, 9, 26, tzinfo=timezone.utc),
        percent=2.0,
    )
    account.observation = UsageObservation.unavailable("claude_oauth_api", "Token expired.")

    view = account.masked()
    assert view["status"] == "unavailable"
    assert view["error_message"] == "Token expired."
    assert view["session_percent_used"] is None
    assert view["weekly_percent_used"] is None
    assert view["weekly_breakdown"] is None
    assert view["usage"]["windows"] == []


# ---------------------------------------------------------------------------
# Defect 1: no formatted duration is ever persisted
# ---------------------------------------------------------------------------

_DURATION_TEXT = re.compile(
    r"resets?\s+(in|at|on)\b|\b\d+\s*(d|h|m|hr|min|day|hour|minute)s?\s+\d+", re.IGNORECASE
)


def test_no_persisted_field_holds_a_formatted_duration():
    """A stored duration is the defect itself, so nothing stored may look like one."""
    # masked() renders against the real clock, so the window has to be open now
    # for the projected countdown to be the thing under test.
    now = datetime.now(timezone.utc)
    account = UsageAccount(provider="claude", name="test")
    account.observation = _observation(now, now + timedelta(hours=5))

    stored = json.dumps(
        account.model_dump(), default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o)
    )
    offenders = _DURATION_TEXT.findall(stored)
    assert not offenders, f"a formatted duration reached the stored record: {offenders}"

    # The same figure rendered for a human does carry one, which is the point:
    # it is produced at read time and thrown away. Matched as a shape, because
    # the exact minute depends on how long the assertion above took.
    view = account.masked()
    assert re.fullmatch(r"Resets in [45]h \d{1,2}m", view["session_reset_time"]), view[
        "session_reset_time"
    ]


# ---------------------------------------------------------------------------
# Defect 4: a window carries one unit, never two
# ---------------------------------------------------------------------------


def test_a_window_cannot_report_a_percentage_and_a_token_count():
    """One unit per window, so a card cannot state both for one thing.

    The Claude card used to show "2% used" beside "370,216,622 tokens". They
    measured different things over different scopes.
    """
    tokens = UsageWindow(key="session", label="Counted", unit=UNIT_TOKENS, used=370216622.0)
    assert tokens.percent_used is None, "a token count with no limit has no percentage"
    assert tokens.has_bar is False, "no denominator means no bar"

    view = render_observation(
        UsageObservation(
            source="claude_transcripts",
            confidence=CONFIDENCE_DERIVED,
            windows=[tokens],
        )
    )
    row = view["windows"][0]
    assert row["amount_text"] == "370,216,622 tokens"
    assert row["percent_used"] is None
    assert row["has_bar"] is False


def test_a_balance_is_money_and_draws_no_bar():
    window = UsageWindow(
        key="balance", label="Balance", unit=UNIT_CURRENCY, used=12.5, currency="USD"
    )
    row = render_observation(
        UsageObservation(source="deepseek_api", confidence=CONFIDENCE_MEASURED, windows=[window])
    )["windows"][0]
    assert row["amount_text"] == "USD 12.50"
    assert row["has_bar"] is False


def test_percent_is_only_derived_from_a_limit_the_source_gave():
    assert UsageWindow(key="k", label="l", unit=UNIT_TOKENS, used=50.0, limit=200.0).percent_used == 25.0
    assert UsageWindow(key="k", label="l", unit=UNIT_TOKENS, used=50.0).percent_used is None


# ---------------------------------------------------------------------------
# Defect 5: dispatch is exact, and the dropdown matches the registry
# ---------------------------------------------------------------------------


def test_antigravity_is_a_registered_provider():
    """AntiGravity had no adapter, so it silently became an Ollama probe."""
    assert is_known("antigravity")
    assert get_adapter("antigravity").provider == "antigravity"


def test_an_unknown_provider_raises_rather_than_falling_through():
    """The old chain ended at the local-harness probe for anything unmatched."""
    with pytest.raises(UnknownProvider) as excinfo:
        get_adapter("mistral")
    assert "mistral" in str(excinfo.value)


def test_a_provider_name_is_not_matched_by_substring():
    """"claude-ish" must not be served by the Claude adapter."""
    for name in ("claude-code", "my-chatgpt", "gemini-pro", "ollama"):
        assert not is_known(name), f"{name} must not resolve by substring"


def test_registry_and_dropdown_cannot_disagree():
    """Every option has an adapter and every adapter has an option.

    The dropdown was a hand-written list in the page, which is how AntiGravity
    came to be absent from the Usage tab while being named everywhere else.
    """
    options = {option["value"] for option in provider_options()}
    assert options == set(PROVIDERS)
    for option in provider_options():
        assert option["label"], f"{option['value']} has no label for the dropdown"


def test_the_page_no_longer_hardcodes_the_provider_list():
    """The markup must not carry its own copy of the options."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    select_start = html.index('<select id="acc-provider"')
    select_end = html.index("</select>", select_start)
    assert "<option" not in html[select_start:select_end], (
        "the provider dropdown has hardcoded options again, so it can drift "
        "out of step with the adapter registry"
    )


# ---------------------------------------------------------------------------
# Defect 3: the sync snippets must reach a real account on this hub
# ---------------------------------------------------------------------------


def test_a_snippet_posts_to_the_account_it_was_built_for():
    snippet = build_sync_snippet("claude", "claude-d991e2", "http://127.0.0.1:9999", "tok-abc")
    assert "/api/usage/accounts/claude-d991e2/telemetry" in snippet
    assert "http://127.0.0.1:9999" in snippet
    assert "tok-abc" in snippet
    assert "X-AgnView-Token" in snippet


def test_a_snippet_does_not_hardcode_a_port_or_omit_the_token():
    for provider in ("claude", "chatgpt", "gemini"):
        snippet = build_sync_snippet(provider, "acc-1", "http://example:1234", "t")
        assert "localhost:8765" not in snippet, f"{provider} snippet hardcodes a port"
        assert "X-AgnView-Token" in snippet, f"{provider} snippet sends no token"


def test_a_snippet_reports_a_failure_instead_of_claiming_success():
    """Every POST used to sit in a silent catch that fell through to success."""
    snippet = build_sync_snippet("claude", "acc-1", "http://h", "t")
    assert "refused the sync" in snippet
    assert "res.status" in snippet


def test_the_page_holds_no_hardcoded_account_ids():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for dead_id in ("claude-d72ee8", "chatgpt-08898b", "gemini-27e447"):
        assert dead_id not in html, f"{dead_id} is back in the page"


def test_a_provider_with_no_usage_page_gets_no_snippet():
    assert build_sync_snippet("antigravity", "acc", "http://h", "t") is None
    assert build_sync_snippet("custom", "acc", "http://h", "t") is None


# ---------------------------------------------------------------------------
# The telemetry payload
# ---------------------------------------------------------------------------


def test_a_reset_countdown_sent_as_text_is_discarded():
    """A countdown string cannot be turned back into an instant, so it is dropped."""
    payload = UsageTelemetryPayload(
        session_percent_used=35.0,
        session_reset_time="Resets in 6d 22h",
        weekly_percent_used=38.0,
        weekly_reset_time="Resets in 6d 22h",
    )
    windows = payload.to_windows()
    assert [w.key for w in windows] == ["session", "week"]
    assert all(w.window_end is None for w in windows)


def test_the_windows_shape_is_preferred_when_present():
    payload = UsageTelemetryPayload(
        windows=[
            {
                "key": "session",
                "label": "Current session",
                "percent_used": 35.0,
                "window_end": "2026-09-22T20:30:00Z",
            }
        ],
        session_percent_used=99.0,
    )
    windows = payload.to_windows()
    assert len(windows) == 1
    assert windows[0].percent_used == 35.0
    assert windows[0].window_end == "2026-09-22T20:30:00Z"


# ---------------------------------------------------------------------------
# Unavailable carries no figures at all
# ---------------------------------------------------------------------------


def test_unavailable_carries_no_window_so_no_figure_can_leak():
    """The old code blanked fields by hand and missed weekly_breakdown, leaving
    invented per-model rows under an "unavailable" summary."""
    observation = UsageObservation.unavailable(
        "gemini_code_assist", "The Google access token has expired.", plan=PlanInfo(name="Gemini")
    )
    view = render_observation(observation)
    assert view["confidence"] == CONFIDENCE_UNAVAILABLE
    assert view["windows"] == []
    assert view["error"] == "The Google access token has expired."
    assert view["plan_name"] == "Gemini"


def test_confidence_distinguishes_a_measurement_from_a_derivation():
    """A counted token total must not read as the provider's own accounting."""
    counted = UsageObservation(
        source="claude_transcripts",
        confidence=CONFIDENCE_DERIVED,
        windows=[UsageWindow(key="session", label="Counted", unit=UNIT_TOKENS, used=1.0)],
    )
    assert counted.is_measured is False
    assert render_observation(counted)["confidence"] == CONFIDENCE_DERIVED
