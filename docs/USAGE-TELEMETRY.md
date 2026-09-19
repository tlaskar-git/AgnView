# Usage telemetry: the one-time browser sync

Some providers publish no quota figure this machine can read. For those, the
Usage tab shows a token count or says "unavailable" until a browser console
script reads the provider's own usage panel and posts the real numbers to
`POST /api/usage/accounts/{id}/telemetry`.

| Provider | Real percentage without a sync | Where the sync script runs |
|---|---|---|
| ChatGPT / Codex | Yes, from `/backend-api/wham/usage` | Not needed |
| Claude | No | Browser console on claude.ai, settings, usage |
| Gemini (Antigravity) | No | Antigravity's own DevTools console |
| DeepSeek | Balance only, from its account API | Not needed |

A card whose provider can hold a synced percentage and holds none shows a
call to action with a button that copies that provider's script, already
pointed at that account's id. The hub computes this as `needs_telemetry_sync`
on the account, so the rule lives in one place and is tested.

A synced window is preserved across later automatic refreshes for as long as
the window it describes stays open: five hours for the session window, seven
days for the weekly one. After that the figure expires and the card asks for a
new sync.

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
