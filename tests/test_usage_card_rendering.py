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
        for name in ("escapeHtml", "formatTokenCount", "renderAccountCard")
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
    """Shape matching what usage_fetcher._fetch_claude_live() writes for a
    heavy, locally-measured Claude Code account: no plan percentage, a long
    descriptive sub-line, and a large token count, for both windows.
    """
    return {
        "provider": "claude",
        "name": "Test User · Max",
        "status": "active",
        "error_message": None,
        "plan_name": "Max",
        "plan_label": "Max 20x",
        "session_title": "Last 5 hours, this machine",
        "session_reset_time": "433 turns across 30 projects, subagents included",
        "session_tokens_used": 119_100_000,
        "session_percent_used": None,
        "session_percent_left": None,
        "percent_used": None,
        "weekly_title": "Last 7 days, this machine",
        "weekly_reset_time": "16692 turns across 30 projects, subagents included",
        "weekly_tokens_used": 3_570_000_000,
        "weekly_percent_used": None,
        "weekly_percent_left": None,
        "weekly_breakdown": None,
        "last_synced_at": None,
    }


@pytest.mark.skipif(not NODE_AVAILABLE, reason="node is required to execute the template's client-side JS")
def test_claude_card_session_and_weekly_blocks_render_as_distinct_values(tmp_path):
    html = _render_claude_card(tmp_path, _claude_account_like_the_bug_report())

    session_sub_line = "433 turns across 30 projects, subagents included"
    weekly_sub_line = "16692 turns across 30 projects, subagents included"
    session_tokens_label = "119.1M tokens"
    weekly_tokens_label = "3.6B tokens"

    # Both figures are present at all (a prerequisite, not the regression itself).
    assert session_sub_line in html
    assert weekly_sub_line in html
    assert session_tokens_label in html
    assert weekly_tokens_label in html

    # The regression: the sub-line used to sit in a `shrink-0` span with no
    # truncation, crammed beside the value in the same row, so a long
    # sub-line overflowed and drew over the value. That exact markup pattern
    # must not come back.
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

    # And each sub-line's row is a distinct element from the row carrying its
    # value -- the value is never found immediately adjacent to the sub-line
    # text inside one shared element.
    session_row_start = html.index(session_sub_line)
    assert session_tokens_label not in html[max(0, session_row_start - 5):session_row_start]
    weekly_row_start = html.index(weekly_sub_line)
    assert weekly_tokens_label not in html[max(0, weekly_row_start - 5):weekly_row_start]
