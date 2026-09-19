"""The Antigravity telemetry extractor, run against text it will really meet.

Antigravity publishes no local usage file and no endpoint, so the only real
figure comes from the panel its own model picker draws. These tests take the
script the Usage tab actually hands out, run it in Node against the text of that
panel, and check the payload it posts. Nothing here invents a percentage: the
input is the wording the panel shows, and every number asserted is derived from
it by the same arithmetic the card will display.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INDEX = Path("agent_relay/web/templates/index.html")

# The operator's model picker, one group after the other. The panel reports what
# is LEFT, so 91% remaining is 9% used.
INLINE_PANEL = "\n".join(
    [
        "Gemini Models",
        "Weekly Limit Remaining 100%",
        "Five Hour Limit Remaining 91%",
        "Claude and GPT models",
        "Weekly Limit Remaining 100%",
        "Five Hour Limit Remaining 85%",
    ]
)

# The same panel with each figure on its own line, which is how a stacked
# layout reads out of innerText.
STACKED_PANEL = "\n".join(
    [
        "Gemini Models",
        "Weekly Limit Remaining",
        "100%",
        "Five Hour Limit Remaining",
        "91%",
        "Claude and GPT models",
        "Weekly Limit Remaining",
        "100%",
        "Five Hour Limit Remaining",
        "85%",
    ]
)

_HARNESS = r"""
const fs = require('fs');
const LF = String.fromCharCode(10);
const CR = String.fromCharCode(13);
const html = fs.readFileSync(process.argv[2], 'utf8').split(CR + LF).join(LF);
const start = html.indexOf('    const TELEMETRY_HUB_ORIGIN');
const endMarker = '      return script;' + LF + '    }';
const end = html.indexOf(endMarker, start) + endMarker.length;
if (start < 0 || end < endMarker.length) {
  throw new Error('buildTelemetryExtractorScript not found in the template');
}
const builder = new Function(html.slice(start, end) + LF + ' return buildTelemetryExtractorScript;')();
const script = builder(process.argv[3], process.argv[4]);

const posted = [];
global.document = { body: { innerText: fs.readFileSync(process.argv[5], 'utf8') } };
global.alert = () => {};
global.console = { log: () => {}, error: () => {} };
global.fetch = async (url, opts) => {
  posted.push({ url: url, body: JSON.parse(opts.body) });
  return { ok: true };
};
(async () => {
  await eval(script);
  process.stdout.write(JSON.stringify(posted));
})();
"""


def _run_extractor(tmp_path: Path, provider: str, account_id: str, panel_text: str):
    """Build the real extractor the UI hands out, then run it over panel_text."""
    if shutil.which("node") is None:
        pytest.skip("node is not available")
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    panel = tmp_path / "panel.txt"
    panel.write_text(panel_text, encoding="utf-8")

    proc = subprocess.run(
        ["node", str(harness), str(INDEX), provider, account_id, str(panel)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("panel", [INLINE_PANEL, STACKED_PANEL], ids=["inline", "stacked"])
def test_extractor_reads_both_groups_and_both_windows(tmp_path, panel):
    posted = _run_extractor(tmp_path, "gemini", "gemini-test", panel)

    assert len(posted) == 1
    assert posted[0]["url"].endswith("/api/usage/accounts/gemini-test/telemetry")
    payload = posted[0]["body"]

    # Both groups the panel shows, in the order it shows them. Reading only the
    # first group is the failure this locks out.
    assert [row["group"] for row in payload["weekly_breakdown"]] == [
        "Gemini Models",
        "Claude and GPT models",
    ]
    assert [row["group"] for row in payload["session_breakdown"]] == [
        "Gemini Models",
        "Claude and GPT models",
    ]

    # "Remaining" means what is left, so the used share is the complement.
    assert payload["weekly_breakdown"][0]["weekly_percent_used"] == 0
    assert payload["weekly_breakdown"][0]["weekly_percent_left"] == 100
    assert payload["session_breakdown"][0]["session_percent_used"] == 9
    assert payload["session_breakdown"][0]["session_percent_left"] == 91
    assert payload["session_breakdown"][1]["session_percent_used"] == 15
    assert payload["session_breakdown"][1]["session_percent_left"] == 85

    # The labels are the panel's own wording, not a name made up here.
    assert payload["session_breakdown"][0]["session_title"] == "Five Hour Limit Remaining"
    assert payload["weekly_breakdown"][0]["weekly_title"] == "Weekly Limit Remaining"

    # The headline figure is the group closest to its limit.
    assert payload["session_percent_used"] == 15
    assert payload["weekly_percent_used"] == 0


def test_extractor_posts_nothing_when_the_panel_is_not_on_screen(tmp_path):
    """A window with no usage panel produces no payload, not a zero."""
    posted = _run_extractor(
        tmp_path, "gemini", "gemini-test", "Antigravity\nOpen a workspace to begin"
    )
    assert posted == []


def test_extractor_skips_a_window_whose_wording_says_neither_used_nor_left(tmp_path):
    """A bare "Weekly Limit 40%" cannot be read either way, so it is dropped."""
    panel = "\n".join(
        [
            "Gemini Models",
            "Weekly Limit 40%",
            "Five Hour Limit Remaining 91%",
        ]
    )
    payload = _run_extractor(tmp_path, "gemini", "gemini-test", panel)[0]["body"]

    assert "weekly_breakdown" not in payload
    assert payload["session_breakdown"][0]["session_percent_used"] == 9


def test_the_card_asks_for_the_account_it_belongs_to(tmp_path):
    """A card's own id reaches the script, so a sync lands on the right account."""
    posted = _run_extractor(tmp_path, "gemini", "gemini-abc123", INLINE_PANEL)
    assert posted[0]["url"].endswith("/api/usage/accounts/gemini-abc123/telemetry")


def test_the_add_account_helper_still_offers_every_provider_a_script():
    """Choosing a provider in the helper must never hand out an empty string."""
    source = INDEX.read_text(encoding="utf-8")
    start = source.index("    const TELEMETRY_HUB_ORIGIN")
    end = source.index("      return script;", start)
    builder = source[start:end]
    for provider in ("claude", "chatgpt", "gemini", "deepseek"):
        assert f"provider === '{provider}'" in builder
    # Every branch posts to the computed endpoint rather than a fixed account.
    assert "http://localhost:8765/api/usage/accounts/" not in builder
    assert len(re.findall(r"\$\{endpoint\}", builder)) >= 3
