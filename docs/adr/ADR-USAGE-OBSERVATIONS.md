# ADR-USAGE-OBSERVATIONS: rebuild usage on observations, not presentations

Status: accepted, delivered 2026-09-22
Date: 2026-09-22
Supersedes: the usage behaviour in `agent_relay/core/usage_fetcher.py`,
`agent_relay/core/claude_code_usage.py`, the `usage_accounts` table columns,
and the browser telemetry snippets in `agent_relay/web/templates/index.html`.

## Context

The Usage tab has never reported correct figures. The Claude card shows wrong
numbers, AntiGravity is absent, and Gemini reports nothing. This ADR records
what was measured on a live hub, names the root causes, and sets the design
that replaces them.

### What the live hub returns today

Read from `GET /api/usage/accounts` on 2026-09-22, three accounts configured.

| Account | Reported | Age of the figure |
|---|---|---|
| `claude-d991e2` | weekly 2% used, "Resets in 6d 22h", session 370,216,622 tokens | percentage and countdown synced 2026-09-19, three days old |
| `chatgpt-44d2f7` | session 0%, weekly 6%, "Resets in 5h 0m" | read today, correct in substance |
| `gemini-63387f` | unavailable | read today |
| AntiGravity | no account exists and none can be created | n/a |

## Root causes

### 1. The store holds presentations, so nothing can be recomputed

`usage_accounts` stores `session_reset_time` and `weekly_reset_time` as human
strings. "Resets in 6d 22h" was true when it was written on 19 September. It is
still on the card today. No column anywhere holds the absolute instant a window
ends, so no correct countdown can be derived. Every stored countdown is wrong
one second after the write.

### 2. Stale telemetry is replayed for seven days and re-marked active

`_capture_synced_windows` keeps a manually synced percentage for
`WEEK_WINDOW_DAYS` (seven days) and `_restore_synced_windows` writes it back
over every fresh recompute. It also sets `status = "active"` and clears
`error_message`. A three-day-old figure is therefore presented as current, with
its age nowhere on the card. This is the wrong information the operator sees.

### 3. The browser sync snippets post to account IDs that do not exist

The three snippets in `index.html` are hardcoded:

| Snippet target | Real account on this machine |
|---|---|
| `usage/accounts/claude-d72ee8/telemetry` | `claude-d991e2` |
| `usage/accounts/chatgpt-08898b/telemetry` | `chatgpt-44d2f7` |
| `usage/accounts/gemini-27e447/telemetry` | `gemini-63387f` |

Every POST returns 404. The snippets also hardcode `http://localhost:8765` and
send no pairing token, so a corrected ID would return 401. All three calls sit
inside a silent try/catch, so the failure is invisible and the snippet reports
success by falling through to the clipboard. The manual sync path has never
worked for any provider.

### 4. Two different units are fused into one Claude card

The card mixes a local transcript token sum with a browser-scraped percentage.
`_tokens_in` adds `cache_read_input_tokens` one for one with `output_tokens`,
which is what produces 370,216,622 tokens in a five-hour window. That figure
measures nothing an operator can act on, and it is not a share of a plan limit.

### 5. Provider dispatch is substring matching with a silent fallthrough

`fetch_account_usage` tests `"claude" in provider`, `"chatgpt" in provider`,
and so on, then falls through to `_fetch_custom_live`. The string
`"antigravity"` matches no branch, so an AntiGravity account becomes an Ollama
probe against `localhost:11434`. The Add Account dropdown has no AntiGravity
option, so one cannot be created either. AntiGravity was never wired into usage
at all.

### 6. The best available sources are never called

Two first-party endpoints exist and the code ignores both. Route existence was
confirmed unauthenticated, where 404 means absent and 401 or 429 means present:

| Route | Unauthenticated status | Verdict |
|---|---|---|
| `GET https://api.anthropic.com/api/oauth/usage` | 429 | exists |
| `GET https://api.anthropic.com/api/oauth/profile` | 401 | exists |
| `POST https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` | 401 | exists |
| `GET https://api.anthropic.com/api/claude_code/usage` | 404 | absent |
| `GET https://api.anthropic.com/api/usage` | 404 | absent |

The Claude Code OAuth bearer token sits in `~/.claude/.credentials.json` under
`claudeAiOauth.accessToken`, with `subscriptionType` and `rateLimitTier`
alongside it. That token plus `/api/oauth/usage` is the real source for the
five-hour and seven-day subscription windows. It is what the Claude Code
`/usage` command reads.

Separately, the Claude Code transcripts under `~/.claude/projects` carry no
rate-limit data. A schema dump of a 13,000-record transcript shows `usage` holds
only token counts. No percentage can ever be derived locally, which is why a
transcript sum is the wrong primitive to build the card on.

For AntiGravity, `~/.gemini/antigravity-cli/cli.log` shows a `quota_manager` and
a `GetG1Credits` fetch, served by a language server that logs
`listening on random port at 49793 for HTTP`. That is a real local source. On
this machine it currently answers
`failed to refresh G1 credits: ... You are not logged into Antigravity`, so the
correct result today is unavailable with that reason.

## Decision

### A. Store observations. Compute presentations at render time.

One observation type replaces the flat percent and reset-string columns.

```python
class UsageWindow:
    key: str                  # "session" | "week" | "rate_limit" | "balance"
    label: str                # as the source names it, e.g. "All models"
    unit: str                 # "percent" | "tokens" | "requests" | "currency"
    used: float | None
    limit: float | None       # None means the source gave no denominator
    window_start: datetime | None
    window_end: datetime | None   # absolute instant, never a countdown
    breakdown: list["UsageWindow"]

class UsageObservation:
    account_id: str
    source: SourceId          # which adapter and which tier produced this
    measured_at: datetime     # when the adapter read it
    confidence: str           # "measured" | "derived" | "unavailable"
    windows: list[UsageWindow]
    plan: PlanInfo | None
    error: str | None
```

Rules this enforces:

- A countdown is `window_end - now()`, recomputed on every render.
- No human-formatted duration is ever written to the database.
- `measured_at` is rendered on every card, so age is always visible.
- A window with `limit is None` renders no bar and no percentage.
- Percentages and token counts never share a window.

### B. Provider registry with exact keys and no fallthrough

```python
PROVIDERS = {
    "claude":      ClaudeAdapter,
    "chatgpt":     ChatGPTAdapter,
    "gemini":      GeminiAdapter,
    "antigravity": AntiGravityAdapter,   # new, first class
    "deepseek":    DeepSeekAdapter,
    "custom":      CustomAdapter,
}
```

An unregistered provider is rejected at account creation. It never degrades into
a localhost probe. The dropdown is generated from this registry, so a registered
adapter cannot be missing from the UI and a UI option cannot lack an adapter.

### C. Each adapter declares an ordered source ladder

An adapter tries its sources in order, returns the first that yields a
measurement, and tags the observation with the source used. The card names that
source. Degradation is visible, never silent.

**claude**

1. `oauth_api` - `GET /api/oauth/usage` with the bearer token from
   `~/.claude/.credentials.json`. Real five-hour and seven-day windows with
   absolute reset instants. Confidence `measured`. On macOS the token lives in
   the Keychain, so the adapter reads it there instead of the file. An expired
   token is reported as such and falls through. AgnView does not refresh it:
   writing that file races Claude Code and risks the sign-in.
2. `api_key` - an `sk-ant-` key gives per-minute rate-limit headers. Labelled a
   rate limit, never a plan window.
3. `local_transcripts` - the transcript sum, kept but rescoped. Billed input and
   output are reported separately from cache reads, because adding cache reads
   one for one is what produced the meaningless total. Confidence `derived`,
   unit `tokens`, no limit, no bar.

**chatgpt**

1. `codex_auth` - the existing `chatgpt.com/backend-api/wham/usage` call, which
   is already correct in substance. The one change is to convert
   `reset_after_seconds` into an absolute `window_end` at read time.
2. `api_key` - OpenAI rate-limit headers.

**gemini**

1. `code_assist` - `POST v1internal:loadCodeAssist` with the token from
   `~/.gemini/oauth_creds.json`. Yields the current tier and its published
   limits. The tier is `measured`. Usage against it is `derived` and labelled.
2. `api_key` - an AI Studio key can be checked for validity and nothing more, so
   the result is `unavailable` with that reason.

**antigravity** (new)

1. `local_server` - discover the port from the newest
   `listening on random port at NNNNN for HTTP` line in
   `~/.gemini/antigravity-cli/cli.log`, then query the quota and credits the
   `quota_manager` and `GetG1Credits` serve. Works while `agy` runs and is
   signed in.
2. `log_scrape` - read the last successful quota refresh from `cli.log`, using
   the log timestamp as `measured_at`. Confidence `measured`, visibly aged.
3. Otherwise `unavailable`, carrying the exact reason from the log, which today
   is that no one is signed in to AntiGravity.

**deepseek** and **custom** keep their current behaviour and move onto the
observation model unchanged in substance.

### D. Delete the replay. Keep the manual sync as one more source.

`_capture_synced_windows` and `_restore_synced_windows` are removed. No field is
re-asserted over a fresh read, and `status` and `error_message` are never
overwritten by a stale value.

The browser sync becomes a `browser_sync` source tier, stored as an observation
with its own `measured_at`, competing on recency alone. It wins only when it is
newer than what the API tier returned, and its age always shows.

The snippets are generated server-side per account, carrying the live account
ID, the actual hub origin and port, and the pairing token. They send
`window_end` as an ISO instant computed in the browser, not the countdown text
from the page. A failed POST reports the status instead of falling through to
the clipboard and claiming success.

### E. Tests that would have caught every defect above

| Test | Defect it catches |
|---|---|
| Frozen clock: store an observation, advance three days, assert the rendered countdown moved and the card reads stale | 1, 2 |
| Registry symmetry: every dropdown option resolves to an adapter and every adapter appears in the dropdown | 5 |
| Snippet resolution: each generated snippet POST target is a real account ID on the hub and carries the token | 3 |
| No stored presentation: assert no persisted field matches a duration format | 1 |
| Unit separation: assert no window carries both a percentage and a token count | 4 |

### F. Migration

No SQL migration is needed. `usage_accounts` stores the whole record in a
`data_json` blob, so the observation lives inside it. `UsageAccount` keeps
identity, credential, plan and `base_url`, and gains one `observation` field.

Legacy percent and reset-string fields are dropped on read. They hold no
`window_end`, so no correct countdown can be recovered from them, and the first
refresh repopulates from a real source.

For the transition, `UsageAccount.masked()` projects the observation back into
the legacy flat fields, computing every countdown at that moment from
`window_end`. Nothing formatted is persisted, and the existing front end keeps
working while it is rewritten to read observations directly.

## Sequencing

| Phase | Work | Outcome |
|---|---|---|
| 1 | Observation model, render-time countdowns, delete the replay, Claude on `oauth_api` | The Claude card shows real plan percentages with a live countdown and a visible measurement age |
| 2 | Provider registry, AntiGravity adapter, dropdown generated from the registry | AntiGravity appears and reports, or states why it cannot |
| 3 | Server-generated browser snippets with live IDs, origin and token | Manual sync works for the first time |
| 4 | Gemini `code_assist`, DeepSeek and Custom moved over, the five tests | Every provider on one contract, regressions blocked |

All four phases are delivered. What the Usage tab reported on this machine
immediately afterwards:

| Account | Before | After |
|---|---|---|
| Claude | weekly 2%, session blank, "Resets in 6d 22h" frozen at a 3-day-old sync | session 36%, weekly 39%, scoped Fable 42% marked binding, per-surface split, live countdowns |
| ChatGPT | correct figures, countdown drifting up to 15 minutes | same figures, countdown exact |
| Gemini | unavailable, no reason an operator could act on | unavailable, naming the signed-in address and saying to restart Gemini |
| AntiGravity | absent from the page and uncreatable | present, reporting that AntiGravity is signed out, quoting its own log |

Test count went from 300 to 304 with the old suite ported: `test_usage.py` and
`test_custom_providers.py` rewritten against the adapters,
`test_usage_telemetry.py` replaced by `test_usage_adapters.py`,
`test_usage_card_rendering.py` ported to the observation shape, and
`test_usage_observations.py` added for the five gate tests above.

## Confirmed response shape of `GET /api/oauth/usage`

Called 2026-09-22T17:37Z with the live Claude Code bearer token plus
`anthropic-beta: oauth-2025-04-20`. Status 200. Abridged, with the fields the
adapter reads:

```json
{
  "five_hour":  { "utilization": 35.0, "resets_at": "2026-09-22T20:30:00Z" },
  "seven_day":  { "utilization": 38.0, "resets_at": "2026-09-26T18:00:00Z" },
  "limits": [
    { "kind": "session",        "group": "session", "percent": 35,
      "severity": "normal", "resets_at": "...", "scope": null,
      "is_active": false },
    { "kind": "weekly_all",     "group": "weekly",  "percent": 38,
      "severity": "normal", "resets_at": "...", "scope": null,
      "is_active": false },
    { "kind": "weekly_scoped",  "group": "weekly",  "percent": 42,
      "severity": "normal", "resets_at": "...",
      "scope": { "model": { "display_name": "Fable" } }, "is_active": true }
  ],
  "seven_day_breakdown": {
    "as_of": "2026-09-22T17:37:20Z",
    "window_started_at": "2026-09-19T18:00:00Z",
    "rows": [
      { "key": "claude_code", "display_name": "Claude Code", "percent": 71 },
      { "key": "chat",        "display_name": "Chats",       "percent": 26 },
      { "key": "cowork",      "display_name": "Cowork",      "percent": 3 },
      { "key": "other",       "display_name": "Other",       "percent": 0 }
    ]
  },
  "extra_usage": { "is_enabled": false, "utilization": null },
  "spend": { "percent": 0, "enabled": false,
             "used": { "amount_minor": 0, "currency": "USD", "exponent": 2 } }
}
```

Notes for the adapter:

- Read `limits[]`, not the top-level window objects. It is the normalised view,
  it carries `severity` and `is_active`, and it is the only place the scoped
  per-model limit appears.
- `resets_at` is already an absolute ISO instant, so it maps straight onto
  `window_end` with no conversion.
- `window_started_at` in `seven_day_breakdown` gives `window_start` for the
  weekly windows.
- The response also carries codenamed keys (`nimbus_quill`, `tangelo`,
  `iguana_necktie` and others) which are unreleased or inapplicable limits and
  are almost all null. Ignore every one of them and read `limits[]`.
- `limit_dollars`, `used_dollars` and `remaining_dollars` are null on this plan.
  Map them only when present, and never infer a limit from a null.

What this proves about the current card: the real figures at that moment were
session 35% and weekly 38%, with a scoped Fable limit at 42%. The dashboard was
showing 2% weekly and no session percentage.

## Consequences

Good:

- A countdown cannot go stale, because none is stored.
- A stale figure cannot pose as current, because age is always rendered.
- A missing provider cannot be silently mishandled, because dispatch is exact.
- Claude reports a real share of a real plan limit for the first time.

Costs:

- A schema migration that discards existing usage rows.
- The Claude adapter depends on an undocumented first-party endpoint, which can
  change. The source ladder contains that risk: if `oauth_api` fails, the card
  says so and falls to the token count rather than showing a wrong percentage.
- AntiGravity usage is available only while `agy` runs and is signed in. The card
  states that rather than implying a figure exists.
