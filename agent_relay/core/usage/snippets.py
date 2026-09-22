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
    # Gemini's own site carries no quota figure, and Google no longer serves
    # Code Assist quota to the CLI for personal accounts. The figure is in
    # AntiGravity's model picker, which is where both of these snippets read.
    "gemini": "AntiGravity's model picker (its own DevTools console)",
    "antigravity": "AntiGravity's model picker (its own DevTools console)",
    "deepseek": None,
    "custom": None,
}

# Shared preamble. Posts the payload with the token and reports the real
# outcome, including a failure, instead of swallowing it.
_POST_HELPER = """
  async function send(payload) {
    // The payload is sent exactly as the reader built it. Adding a field here
    // would put this script out of step with the Python parser that reads the
    // same panel, and a test compares the two payloads for equality.
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

# AntiGravity prints one "Weekly Limit Remaining" and one "Five Hour Limit
# Remaining" per model group in its model picker, for its own models and for
# Gemini's. The automatic read in ``agent_relay.core.antigravity`` gets these
# through the app's debug port; this snippet is the fallback for someone who
# wants the figure without that, and it carries the same rules.
#
# Two copies of a rule drift apart, so tests/test_antigravity_cdp.py runs this
# script in Node and the Python parser over the same panel fixtures and fails if
# the two payloads differ. Sending the Python parser's output over the debug
# port instead was not an option here: this runs in the operator's own console,
# with no hub involvement until the POST.
_ANTIGRAVITY_BODY = """
  // Open AntiGravity's model picker first, so the usage panel is on screen and
  // its text is in the DOM this script reads.
  const bodyText = (document.body && document.body.innerText) || '';
  const lines = bodyText.split('\\n').map(function (l) { return l.trim(); }).filter(Boolean);

  // A group heading names a family of models, for example "Gemini Models" or
  // "Claude and GPT models". Every limit line under it belongs to that group
  // until the next heading, so both groups are read, not just the first.
  const isGroupHeading = function (line) {
    return /\\bmodels?\\s*$/i.test(line) && !/%/.test(line)
      && !/limit/i.test(line) && line.length <= 60;
  };

  // The wording says whether the number is what is left or what is spent. A
  // line that says neither is skipped rather than read the wrong way round.
  const limitPattern = /^(five[\\s-]?hour|5[\\s-]?hour|weekly)\\s+limit(?:\\s+(remaining|left|used))?\\b/i;
  const percentOnLine = /(\\d+(?:\\.\\d+)?)\\s*%/;
  const percentAlone = /^(\\d+(?:\\.\\d+)?)\\s*%$/;
  const round1 = function (n) { return Math.round(n * 10) / 10; };

  const groups = [];
  let current = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (isGroupHeading(line)) {
      current = { group: line, windows: {} };
      groups.push(current);
      continue;
    }
    const limitMatch = line.match(limitPattern);
    if (!limitMatch || !current) continue;
    const sense = (limitMatch[2] || '').toLowerCase();
    if (!sense) continue;
    // The figure sits on the label's own line, or on one of the next two lines
    // when the panel puts it underneath.
    let percentMatch = line.match(percentOnLine);
    for (let k = i + 1; !percentMatch && k < lines.length && k <= i + 2; k++) {
      percentMatch = lines[k].match(percentAlone);
    }
    if (!percentMatch) continue;
    const value = parseFloat(percentMatch[1]);
    const used = sense === 'used' ? value : round1(100 - value);
    const left = sense === 'used' ? round1(100 - value) : value;
    const key = /weekly/i.test(limitMatch[1]) ? 'weekly' : 'session';
    current.windows[key] = {
      title: line.replace(percentOnLine, '').trim(), used: used, left: left
    };
  }

  const weeklyRows = [];
  const sessionRows = [];
  groups.forEach(function (g) {
    if (g.windows.weekly) {
      weeklyRows.push({
        group: g.group,
        weekly_title: g.windows.weekly.title,
        weekly_percent_used: g.windows.weekly.used,
        weekly_percent_left: g.windows.weekly.left
      });
    }
    if (g.windows.session) {
      sessionRows.push({
        group: g.group,
        session_title: g.windows.session.title,
        session_percent_used: g.windows.session.used,
        session_percent_left: g.windows.session.left
      });
    }
  });

  // Only what the window actually said. A group or a window that could not be
  // read is left out, so a failed scrape reports nothing rather than a guess.
  if (weeklyRows.length === 0 && sessionRows.length === 0) {
    alert('[AgnView] Nothing was read from this window. Open the model picker '
          + 'so the usage panel is on screen, then run this again.');
    return;
  }

  const payload = { provider: 'gemini' };
  if (weeklyRows.length > 0) {
    payload.weekly_breakdown = weeklyRows;
    // The headline figure is the group closest to its limit, so the single
    // number on the card is the one that actually constrains the account. It is
    // picked from the rows above, never computed out of nothing.
    const tightest = weeklyRows.reduce(function (a, b) {
      return b.weekly_percent_used > a.weekly_percent_used ? b : a;
    });
    payload.weekly_title = tightest.weekly_title;
    payload.weekly_percent_used = tightest.weekly_percent_used;
    payload.weekly_percent_left = tightest.weekly_percent_left;
  }
  if (sessionRows.length > 0) {
    payload.session_breakdown = sessionRows;
    const tightest = sessionRows.reduce(function (a, b) {
      return b.session_percent_used > a.session_percent_used ? b : a;
    });
    payload.session_title = tightest.session_title;
    payload.session_percent_used = tightest.session_percent_used;
    payload.session_percent_left = tightest.session_percent_left;
  }

  await send(payload);
"""

_BODIES = {
    "claude": _CLAUDE_BODY,
    "chatgpt": _CHATGPT_BODY,
    # Both read AntiGravity's model picker, because that is where the figures
    # for both actually are.
    "gemini": _ANTIGRAVITY_BODY,
    "antigravity": _ANTIGRAVITY_BODY,
}


def build_sync_snippet(
    provider: str, account_id: str, origin: str, token: str
) -> Optional[str]:
    """The snippet for this account, or None when there is nothing to read.

    DeepSeek and a local harness have no page: their adapters read a real
    source directly, so there is never a script to paste for them.
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
