# Usage telemetry: live where a credential allows it, a one-time sync otherwise

Some providers publish no quota figure this machine can read on its own. For
those, the Usage tab shows a token count or says "unavailable" until either a
person's own pasted session credential is used to call the provider's own API
directly (live, on every refresh), or a browser console script reads the
provider's own usage panel once and posts the real numbers to
`POST /api/usage/accounts/{id}/telemetry` (a snapshot that expires).

| Provider | Real percentage without any pasted credential | Live on every refresh | One-off sync |
|---|---|---|---|
| ChatGPT / Codex | Yes, from `/backend-api/wham/usage` | Always | Not needed |
| Claude | No | Yes, if a claude.ai session cookie is pasted into the account's credential (Authentication Method: Session Token) | Browser console on claude.ai, settings, usage |
| Gemini (Antigravity) | No | Yes, while Antigravity is running (its own local debug port, see below) | Antigravity's own DevTools console, advanced builds only |
| DeepSeek | Balance only, from its account API | Always | Not needed |

## Claude: a pasted session cookie calls claude.ai's own API directly

`_fetch_claude_web_session` in `agent_relay/core/usage_fetcher.py` is tried
whenever a Claude account's credential is not an `sk-ant-` API key and not this
app's own `claude-cli-` placeholder for a locally detected CLI session. The
credential is sent as a `Cookie` header (a bare value is wrapped as
`sessionKey=<value>`, so either the sessionKey alone from DevTools' Application
panel or the full Cookie header from its Network tab works) to:

1. `GET https://claude.ai/api/organizations` to find the account's own
   organisation id. Not read from `~/.claude.json`, because a person who only
   ever uses claude.ai in a browser, never the Claude Code CLI, has no such
   file.
2. `GET https://claude.ai/api/organizations/{uuid}/usage`, whose `limits` array
   is mapped directly: `kind: "session"` to the session window, `kind:
   "weekly_all"` to the weekly window, and each `kind: "weekly_scoped"` entry to
   one row in `weekly_breakdown`, labelled from `scope.model.display_name` (for
   example "Fable").

This is the same request claude.ai's own settings page makes to draw its own
usage panel, captured live from a real, logged-in browser session. A cookie
that is confirmed rejected (401/403) marks the account unavailable with a
specific "sign in again and paste a fresh one" reason, since that is something
the person can act on. Any other failure (network error, an unmapped response
shape) falls back to the local transcript token count silently, so a passing
network hiccup never blanks a figure that already works.

Nothing here reads a cookie out of a browser's own storage. The person copies
their own session cookie out of their own browser's DevTools and pastes it into
this app's own credential field, the same as pasting an API key for any other
provider. AgnView is heading to public, freeware distribution, and a general
purpose Chromium cookie decryptor was deliberately not built for exactly that
reason: it would silently read a stranger's session the moment they opened this
app, with no specific action or consent on their part. A pasted credential
requires that person's own deliberate action every time, for their own account
only.

A card whose provider can hold a synced percentage and holds none shows a
call to action with a button that copies that provider's one-off sync script,
already pointed at that account's id. The hub computes this as
`needs_telemetry_sync` on the account, so the rule lives in one place and is
tested. Once a live credential path (Claude's pasted cookie, or Antigravity
running) is working, that condition is false and the card stops asking.

A one-off synced window (no live credential in play) is preserved across later
automatic refreshes for as long as the window it describes stays open: five
hours for the session window, seven days for the weekly one. After that the
figure expires and the card asks for a new sync.

## Antigravity reads itself, while it is running

Antigravity needs no paste. Every refresh of a Gemini or Antigravity account
asks the app's own debug port for the text on its screen, parses the model
picker exactly as the pasted script does, and fills the same fields:

1. `agent_relay/core/antigravity.py` reads the first line of
   `%APPDATA%\Antigravity\DevToolsActivePort` (on macOS
   `~/Library/Application Support/Antigravity/`, on Linux
   `$XDG_CONFIG_HOME/Antigravity/`).
2. It opens a TCP connection to that port on 127.0.0.1. A port file left behind
   by a closed app names a port nothing answers on, so this connection, not the
   file, is what says the app is running.
3. `GET /json/list` names the window, then one `Runtime.evaluate` over the
   DevTools Protocol websocket reads `document.body.innerText`.
4. The panel text is parsed and written to the account as a measured window,
   stamped the same way a pasted sync is.

It never starts Antigravity. A routine usage refresh that launched an
application would be intrusive, so when the app is closed the card says
"AntiGravity needs to be running for an automatic usage read" and the pasted
script stays available for a figure without it. A read that fails for any other
reason says which step failed, and the card falls back to unavailable rather
than raising.

The parsing rules are written once, in `parse_usage_panel`. The pasted script
carries them in JavaScript as well, so `tests/test_antigravity_cdp.py` runs both
over the same panel fixtures and fails if the two ever disagree.

Nothing here reads a browser cookie store, a browser profile or any stored
credential. It is the app's own loopback debug interface, describing the app's
own window, reading text already on screen.

## Opening DevTools in Antigravity

Antigravity is an Electron app, but the installed build does not give you an
in-window DevTools panel:

- `resources/app.asar` `dist/utils.js` creates the window with
  `devTools: !app.isPackaged`, so DevTools is off in the packaged build.
- `dist/menu.js` walks the menu and hides every item whose role is
  `toggleDevTools`, so Help has no Toggle Developer Tools entry. The preload
  bridge still exposes `toggleDevTools()`, but with `devTools: false` it does
  nothing.

What does work is Chromium's remote debugging endpoint, which the app turns on
for itself. `dist/main.js` appends `remote-debugging-port=0` whenever the
switch is not already set, so every run listens on an OS-assigned port and
Chromium writes that port to the first line of
`%APPDATA%\Antigravity\DevToolsActivePort`.

To reach the console:

1. Start Antigravity and sign in.
2. Read the first line of `%APPDATA%\Antigravity\DevToolsActivePort`. That is
   the port. Passing `--remote-debugging-port=9222` on the command line pins it
   to a port of your choosing instead.
3. Open `http://127.0.0.1:<port>` in Chrome, or use `chrome://inspect`, and
   open the Antigravity page target. That is a full DevTools window with a
   Console.
4. Open Antigravity's model picker so the usage panel is on screen.
5. Paste the extractor script from the Usage tab card and press Enter.

Verified on Antigravity 2.15.0 (Electron 41.10.3, Chrome 146): the target is
listed, and `Runtime.evaluate` runs JavaScript inside the window.

## What the Antigravity extractor reads

The model picker prints one "Weekly Limit Remaining" and one "Five Hour Limit
Remaining" per model group, for example "Gemini Models" and "Claude and GPT
models". The script reads every group the panel shows, not just the first, and
converts "Remaining" to a used share. A group or a window it cannot read is
left out of the payload. It never fills a gap with a guess.

The weekly rows go to `weekly_breakdown` and the five-hour rows to
`session_breakdown`. They are kept apart because the two windows expire on
different clocks.
