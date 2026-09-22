"""Regression test for the Claude usage card's overlapping stat blocks.

The operator reported the Claude (Anthropic) card on the dashboard rendering
"garbled", overlapping text: the session block's turn/project sub-line
("433 turns across 30 projects, subagents included") drawn on top of its own
token count ("119.1M tokens"), and the same collision in the weekly block
("16692 turns across 30 projects, subagents included" over "3.6B tokens").

Root cause: ``renderAccountCard`` in ``agent_relay/web/templates/index.html``
put the session/weekly sub-line (``session_reset_time`` /
``weekly_reset_time``, which ``usage_fetcher._scope_sub_line`` fills with a
long descriptive string for a locally-measured Claude Code account) in a
``shrink-0`` span squeezed into the same flex row as the truncating title and
the token-count value. A ``shrink-0`` flex item never shrinks below its
content width and was given no ``truncate``/wrap allowance, so a long
sub-line overflowed its row and rendered over the value next to it.

This test extracts the real ``renderAccountCard`` (plus the small helpers it
calls) straight out of the shipped template and executes it under Node with
data shaped exactly like the reported bug, then asserts the sub-line and the
value land in distinct, non-overflowing elements. It fails on the pre-fix
markup and passes on the fix.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE_PATH = Path("agent_relay/web/templates/index.html")

NODE_AVAILABLE = shutil.which("node") is not None


def _extract_function(source: str, name: str) -> str:
    """Pull one top-level ``function name(...) { ... }`` out of the template.

    Uses a plain balanced-brace scan starting at the function's own opening
    brace. That is enough here because none of the functions this test
    extracts contain a literal, unbalanced ``{`` or ``}`` inside a plain
    string (only inside ``${...}`` template-literal expressions, whose own
    braces are already balanced).
    """
    marker = f"function {name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    i = brace_start
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                i += 1
                break
    else:
        raise AssertionError(f"unbalanced braces while extracting {name}")
    return source[start:i]


def _render_claude_card(tmp_path: Path, account: dict) -> str:
    """Run the real renderAccountCard() under Node and return the HTML it built."""
    source = TEMPLATE_PATH.read_text(encoding="utf-8")
    js_functions = "\n\n".join(
        _extract_function(source, name)
        for name in (
            "escapeHtml",
            "formatTokenCount",
            "usageWindowColours",
            "renderUsageWindowRow",
            "renderUsageWindowsHtml",
            "usageProvenanceHtml",
            "renderAccountCard",
        )
    )
    driver = f"""
{js_functions}

// renderAccountCard() only calls renderProgressRing() on the Gemini branch,
// which this test does not exercise, but the name must resolve.
function renderProgressRing() {{ return ''; }}

const account = {json.dumps(account)};
process.stdout.write(renderAccountCard(account));
"""
    script_path = tmp_path / "render_card.js"
    script_path.write_text(driver, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, f"renderAccountCard() threw:\n{result.stderr}"
    return result.stdout


def _claude_account_like_the_bug_report() -> dict:
    """A heavy, locally-counted Claude Code account.

    Two token windows with no limit, each carrying a long descriptive
    sub-line. That combination is what produced the overlapping text: the
    sub-line was drawn on top of the token count beside it.
    """
    session_sub = "433 turns across 30 projects, subagents included"
    weekly_sub = "16692 turns across 30 projects, subagents included"
    return {
        "provider": "claude",
        "name": "Test User \u00b7 Max",
        "status": "active",
        "error_message": None,
        "plan_name": "Max",
        "plan_label": "Max 20x",
        "last_synced_at": None,
        "usage": {
            "source": "claude_transcripts",
            "source_label": "Counted from local transcripts",
            "confidence": "derived",
            "measured_at": "2026-09-22T17:00:00+00:00",
            "age_seconds": 12.0,
            "age_text": "measured 12s ago",
            "is_stale": False,
            "error": None,
            "plan_name": "Max",
            "plan_label": "Max 20x",
            "windows": [
                {
                    "key": "session",
                    "label": "Counted, last 5 hours on this machine",
                    "sub_label": session_sub,
                    "unit": "tokens",
                    "amount_text": "119,100,000 tokens",
                    "percent_used": None,
                    "has_bar": False,
                    "used": 119100000.0,
                    "limit": None,
                    "severity": None,
                    "is_active": False,
                    "window_start": None,
                    "window_end": None,
                    "countdown_text": None,
                    "breakdown": [],
                },
                {
                    "key": "week",
                    "label": "Counted, last 7 days on this machine",
                    "sub_label": weekly_sub,
                    "unit": "tokens",
                    "amount_text": "3,570,000,000 tokens",
                    "percent_used": None,
                    "has_bar": False,
                    "used": 3570000000.0,
                    "limit": None,
                    "severity": None,
                    "is_active": False,
                    "window_start": None,
                    "window_end": None,
                    "countdown_text": None,
                    "breakdown": [],
                },
            ],
        },
    }


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node is required to execute the template's client-side JS")
def test_claude_card_sub_line_and_value_render_as_distinct_elements(tmp_path):
    html = _render_claude_card(tmp_path, _claude_account_like_the_bug_report())

    session_sub_line = "433 turns across 30 projects, subagents included"
    weekly_sub_line = "16692 turns across 30 projects, subagents included"
    session_tokens_label = "119,100,000 tokens"
    weekly_tokens_label = "3,570,000,000 tokens"

    # Both figures are present at all (a prerequisite, not the regression).
    assert session_sub_line in html
    assert weekly_sub_line in html
    assert session_tokens_label in html
    assert weekly_tokens_label in html

    # The regression: the sub-line used to sit in a `shrink-0` span with no
    # truncation, crammed beside the value in the same row, so a long sub-line
    # overflowed and drew over the value. That markup must not come back.
    assert f'shrink-0">{session_sub_line}' not in html, (
        "session sub-line is back in a non-shrinking span next to its value "
        "-- this is the overlapping-text bug"
    )
    assert f'shrink-0">{weekly_sub_line}' not in html, (
        "weekly sub-line is back in a non-shrinking span next to its value "
        "-- this is the overlapping-text bug"
    )

    # The fix: each sub-line renders in its own truncating, full-width row.
    assert f'truncate mb-1.5">{session_sub_line}</div>' in html
    assert f'truncate mb-1.5">{weekly_sub_line}</div>' in html

    # And the value is never immediately adjacent to the sub-line text inside
    # one shared element.
    session_row_start = html.index(session_sub_line)
    assert session_tokens_label not in html[max(0, session_row_start - 5):session_row_start]
    weekly_row_start = html.index(weekly_sub_line)
    assert weekly_tokens_label not in html[max(0, weekly_row_start - 5):weekly_row_start]


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node is required to execute the template's client-side JS")
def test_a_window_with_no_limit_draws_no_bar(tmp_path):
    """A counted token total has no denominator, so it gets no progress bar.

    Drawing one would imply a limit that nobody published.
    """
    html = _render_claude_card(tmp_path, _claude_account_like_the_bug_report())
    assert "rounded-full transition-all" not in html, (
        "a bar was drawn for a window with no limit"
    )


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node is required to execute the template's client-side JS")
def test_the_card_states_its_source_and_age(tmp_path):
    """Provenance is not optional. A figure with no age passed for a live one."""
    html = _render_claude_card(tmp_path, _claude_account_like_the_bug_report())
    assert "Counted from local transcripts" in html
    assert "measured 12s ago" in html
    # "derived" means this machine worked it out, which the card must say.
    assert "derived, not a plan limit" in html


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node is required to execute the template's client-side JS")
def test_every_window_renders_including_a_scoped_one(tmp_path):
    """All windows render, not just one session and one weekly.

    The flat-field renderer kept exactly one of each, which silently dropped
    the scoped per-model weekly limit -- often the binding one.
    """
    account = _claude_account_like_the_bug_report()
    account["usage"]["source"] = "claude_oauth_api"
    account["usage"]["source_label"] = "Anthropic account usage"
    account["usage"]["confidence"] = "measured"
    account["usage"]["windows"] = [
        {
            "key": "session", "label": "Session, last 5 hours", "sub_label": None,
            "unit": "percent", "amount_text": "35% used", "percent_used": 35.0,
            "has_bar": True, "used": 35.0, "limit": 100.0, "severity": "normal",
            "is_active": False, "window_start": None,
            "window_end": "2026-09-22T20:30:00+00:00",
            "countdown_text": "Resets in 2h 44m", "breakdown": [],
        },
        {
            "key": "week", "label": "Weekly, all models, 7 days", "sub_label": None,
            "unit": "percent", "amount_text": "38% used", "percent_used": 38.0,
            "has_bar": True, "used": 38.0, "limit": 100.0, "severity": "normal",
            "is_active": False, "window_start": None,
            "window_end": "2026-09-26T18:00:00+00:00",
            "countdown_text": "Resets in 4d 0h",
            "breakdown": [
                {
                    "key": "week_surface_claude_code", "label": "Claude Code",
                    "sub_label": None, "unit": "percent", "amount_text": "71% used",
                    "percent_used": 71.0, "has_bar": True, "used": 71.0, "limit": 100.0,
                    "severity": None, "is_active": False, "window_start": None,
                    "window_end": None, "countdown_text": None, "breakdown": [],
                }
            ],
        },
        {
            "key": "week", "label": "Weekly, Fable", "sub_label": None,
            "unit": "percent", "amount_text": "42% used", "percent_used": 42.0,
            "has_bar": True, "used": 42.0, "limit": 100.0, "severity": "warning",
            "is_active": True, "window_start": None,
            "window_end": "2026-09-26T18:00:00+00:00",
            "countdown_text": "Resets in 4d 0h", "breakdown": [],
        },
    ]

    html = _render_claude_card(tmp_path, account)

    assert "Session, last 5 hours" in html
    assert "Weekly, all models, 7 days" in html
    assert "Weekly, Fable" in html, "the scoped weekly limit was dropped again"
    assert "42% used" in html
    # The per-surface split renders under the window it belongs to.
    assert "Claude Code" in html and "71% used" in html
    # The provider says which window is binding, and the card shows it.
    assert "binding" in html
    # Countdowns come from the hub, computed from the absolute instant.
    assert "Resets in 2h 44m" in html
