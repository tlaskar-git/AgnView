"""Browser sync snippets, generated per account.

A snippet reads a provider's own usage page and posts the result to this hub.
Each one is built with the account's real id, this hub's real origin and port,
and the pairing token, because all three were previously hardcoded and all
three were wrong:

  - the account ids in the page were ``claude-d72ee8``, ``chatgpt-08898b`` and
    ``gemini-27e447``, none of which existed on the machine, so every POST
    returned 404
  - the origin was fixed at ``http://localhost:8765``, so a hub on another port
    was unreachable
  - no token was sent, so a corrected id would have returned 401

Every failure sat inside a silent ``try/catch`` that fell through to the
clipboard and reported success, which is why the manual sync appeared to work
and never did.

Two further rules hold here. A snippet sends ``window_end`` as an absolute ISO
instant that it computes from the page, never the page's countdown text. And a
snippet that reads nothing says so and posts nothing, rather than sending a
zero that would render as a real measurement.
"""

import json
from typing import Optional

# Where an operator has to be for each snippet to have anything to read.
USAGE_PAGES = {
    "claude": "https://claude.ai/settings/usage",
    "chatgpt": "https://chatgpt.com/codex/settings/usage",
    "gemini": "https://gemini.google.com/",
    "antigravity": None,
    "deepseek": None,
    "custom": None,
}

# Shared preamble. Posts the payload with the token and reports the real
# outcome, including a failure, instead of swallowing it.
_POST_HELPER = """
  async function send(payload) {
    payload.plan_name = payload.plan_name || null;
    const body = JSON.stringify(payload, null, 2);
    try { if (typeof copy === 'function') copy(body); } catch (e) {}
    console.log('[AgnView] read from this page:', payload);
    let res;
    try {
      res = await fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-AgnView-Token': TOKEN },
        body: body
      });
    } catch (e) {
      alert('[AgnView] Could not reach the hub at ' + ENDPOINT + '\\n\\n' + e
            + '\\n\\nThe reading is on your clipboard.');
      return;
    }
    if (res.ok) {
      alert('[AgnView] Synced to AgnView.');
      return;
    }
    let detail = '';
    try { detail = JSON.stringify(await res.json()); } catch (e) {}
    alert('[AgnView] The hub refused the sync: HTTP ' + res.status + '\\n' + detail
          + '\\n\\nThe reading is on your clipboard.');
  }
"""

# Claude: read the usage page text. A countdown like "2 hours" is turned into an
# absolute instant here, at the moment it is read, so the hub never stores a
# relative value.
_CLAUDE_BODY = """
  const text = document.body.innerText;

  function instantFromRelative(raw) {
    if (!raw) return null;
    let seconds = 0, matched = false;
    const days = raw.match(/(\\d+)\\s*d(ay)?/i);
    const hours = raw.match(/(\\d+)\\s*h(r|our)?/i);
    const mins = raw.match(/(\\d+)\\s*m(in)?/i);
    if (days) { seconds += parseInt(days[1], 10) * 86400; matched = true; }
    if (hours) { seconds += parseInt(hours[1], 10) * 3600; matched = true; }
    if (mins) { seconds += parseInt(mins[1], 10) * 60; matched = true; }
    if (!matched) return null;
    return new Date(Date.now() + seconds * 1000).toISOString();
  }

  const windows = [];
  const session = text.match(/Resets in ([^\\n]+)[\\s\\S]{0,80}?(\\d+)%\\s*used/i);
  if (session) {
    windows.push({
      key: 'session',
      label: 'Current session',
      percent_used: parseFloat(session[2]),
      window_end: instantFromRelative(session[1])
    });
  }

  const rows = [];
  const lines = text.split('\\n').map(function (l) { return l.trim(); });
  for (let i = 0; i < lines.length; i++) {
    const reset = lines[i].match(/^Resets\\s+(.+)$/i);
    if (!reset) continue;
    let label = null;
    for (let j = i - 1; j >= 0 && j >= i - 3; j--) {
      if (!/^Resets/i.test(lines[j]) && !/%\\s*used/i.test(lines[j]) && lines[j]) {
        label = lines[j];
        break;
      }
    }
    let percent = null;
    for (let k = i + 1; k < lines.length && k <= i + 3; k++) {
      const pct = lines[k].match(/(\\d+)%\\s*used/i);
      if (pct) { percent = parseFloat(pct[1]); break; }
    }
    if (label && percent !== null) {
      rows.push({ label: label, percent_used: percent,
                  window_end: instantFromRelative(reset[1]) });
    }
  }
  if (rows.length) {
    const primary = rows.filter(function (r) { return /all models/i.test(r.label); })[0]
                    || rows[0];
    windows.push({
      key: 'week',
      label: 'Weekly limits',
      percent_used: primary.percent_used,
      window_end: primary.window_end,
      breakdown: rows
    });
  }

  if (!windows.length) {
    alert('[AgnView] Nothing was read from this page. Open claude.ai settings, '
          + 'usage, then run this again.');
    return;
  }

  const plan = text.match(/Usage limits\\s+([^\\n]+)/i);
  await send({ plan_label: plan ? plan[1].trim() : null, windows: windows });
"""

# ChatGPT: read the same endpoint Codex reads, which reports offsets in seconds.
# They are anchored to now here.
_CHATGPT_BODY = """
  let data;
  try {
    const res = await fetch('/backend-api/wham/usage');
    if (!res.ok) {
      alert('[AgnView] ChatGPT returned HTTP ' + res.status
            + '. Make sure you are signed in to chatgpt.com.');
      return;
    }
    data = await res.json();
  } catch (e) {
    alert('[AgnView] Could not read ChatGPT usage: ' + e);
    return;
  }

  const rl = (data && data.rate_limit) || {};
  const windows = [];
  function add(key, label, block) {
    if (!block || block.used_percent === null || block.used_percent === undefined) return;
    const secs = block.reset_after_seconds;
    windows.push({
      key: key,
      label: label,
      percent_used: block.used_percent,
      window_end: (typeof secs === 'number' && secs >= 0)
        ? new Date(Date.now() + secs * 1000).toISOString()
        : null
    });
  }
  add('session', '5-hour limit', rl.primary_window);
  add('week', 'Weekly limit', rl.secondary_window);

  if (!windows.length) {
    alert('[AgnView] ChatGPT reported no usage windows for this account.');
    return;
  }
  const plan = (data.plan_type || '').trim();
  await send({
    plan_name: plan ? 'ChatGPT ' + plan : null,
    plan_label: plan || null,
    windows: windows
  });
"""

_GEMINI_BODY = """
  const text = document.body.innerText;

  function instantFromAbsolute(raw) {
    if (!raw) return null;
    const parsed = Date.parse(raw);
    return isNaN(parsed) ? null : new Date(parsed).toISOString();
  }

  const windows = [];
  const current = text.match(/Current usage[\\s\\S]*?Resets at ([^\\n]+)[\\s\\S]*?(\\d+)%\\s*used/i);
  if (current) {
    windows.push({ key: 'session', label: 'Current usage',
                   percent_used: parseFloat(current[2]),
                   window_end: instantFromAbsolute(current[1]) });
  }
  const weekly = text.match(/Weekly limit[\\s\\S]*?Resets on ([^\\n]+)[\\s\\S]*?(\\d+)%\\s*used/i);
  if (weekly) {
    windows.push({ key: 'week', label: 'Weekly limit',
                   percent_used: parseFloat(weekly[2]),
                   window_end: instantFromAbsolute(weekly[1]) });
  }

  if (!windows.length) {
    alert('[AgnView] Nothing was read from this page. Open the Gemini usage page '
          + 'and run this again.');
    return;
  }
  const plan = text.match(/Usage limits\\s+([^\\n]+)/i);
  await send({ plan_label: plan ? plan[1].trim() : null, windows: windows });
"""

_BODIES = {
    "claude": _CLAUDE_BODY,
    "chatgpt": _CHATGPT_BODY,
    "gemini": _GEMINI_BODY,
}


def build_sync_snippet(
    provider: str, account_id: str, origin: str, token: str
) -> Optional[str]:
    """The snippet for this account, or None when the provider has no page.

    AntiGravity, DeepSeek and a local harness publish no usage page worth
    scraping, and their adapters read a real source directly, so no snippet is
    generated for them.
    """
    body = _BODIES.get((provider or "").lower())
    if body is None:
        return None

    endpoint = f"{origin.rstrip('/')}/api/usage/accounts/{account_id}/telemetry"
    return (
        "(async function () {\n"
        f"  const ENDPOINT = {json.dumps(endpoint)};\n"
        f"  const TOKEN = {json.dumps(token or '')};\n"
        f"{_POST_HELPER}"
        f"{body}"
        "})();"
    )
