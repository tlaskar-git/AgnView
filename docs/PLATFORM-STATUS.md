# Platform status

What AgnView does on each platform today, what differs between them, and what
is still to build. Read this first when starting work on a platform that is
not Windows.

## Summary

| Platform | How it runs | Status |
|---|---|---|
| Windows | Desktop app (`AgnView.exe`), or `agnview serve` in a browser | Complete. Released as `AgnView-windows-x64.zip` on every GitHub release |
| macOS | `agnview serve` in a browser | Works. No desktop app yet |
| Linux | `agnview serve` in a browser | Works. No desktop app |
| iOS and iPadOS | Companion app that pairs with a hub | Not built. Design and spec only, in a separate repository |
| Android | Companion app | Not started |

## Windows desktop app

Code: `agent_relay/desktop/`. Build: `tools/build-windows.ps1 -Install`, run
locally with PyInstaller, never on CI.

- A WebView2 window over the hub on loopback, a tray icon, and a single
  instance per user. A second launch brings the open window forward.
- The close button hides the window to the tray. Clicking the tray icon opens
  it again, and Quit AgnView in the tray menu stops the app and the hub.
- Start with Windows is a per-user Task Scheduler sign-in task, off by
  default, shared with the dashboard's autostart switch.
- Port 18845 by default, moving to the next free port when that one is busy.
- Every child process starts without a console window (`agent_relay/core/proc.py`).

## Usage tab sources

Every card needs no setup. Discovery (`agent_relay/core/usage/discover.py`)
adds a card for each signed-in tool at every start and every ten minutes, and
a card the person deletes stays deleted.

| Card | Source | Windows | macOS | Linux |
|---|---|---|---|---|
| Claude | Anthropic account usage API with the Claude Code sign-in. AgnView asks Claude Code to renew it before it expires | `~/.claude/.credentials.json` | Keychain, `Claude Code-credentials` | `~/.claude/.credentials.json` |
| ChatGPT | ChatGPT usage API with the Codex sign-in, `~/.codex/auth.json` | Yes | Yes | Yes |
| AntiGravity and Gemini | Google's quota service with AntiGravity's own sign-in, renewed in memory (`adapters/agy_cloud.py`) | Windows Credential Manager, `gemini:antigravity` | **Not built.** Falls back to reading the app's model picker while AntiGravity is open | Not built. Same fallback |

A card keeps its last real reading, with its age, when a read fails, until
that window resets (`agent_relay/core/usage/service.py`).

## Console

Chat runs each agent's command-line tool, not its desktop app: `claude` for
Claude Code, `codex` for Codex (old and new output formats are read), and
`agy` for AntiGravity. When one is missing, the console says which command is
missing and how to install it. It never answers on the agent's behalf.

## Mobile pairing

The dashboard's phone icon shows a QR code with the hub's name, pairing key,
certificate fingerprint and an iroh ticket. `tools/iroh-client.py` is the
reference client: it connects with that payload over the LAN or over iroh and
streams the console. Tested end to end against the Windows desktop app over
iroh. `docs/PAIRING.md` and `docs/mobile-api-spec.json` are the contract a
phone app must follow.

The Windows desktop app has an Allow phones on my network setting, in the
tray menu and on the pairing screen, off by default. Off, it listens on
loopback and the QR code's `lan` field is `127.0.0.1:<port>`, which a client
skips, so the phone goes straight to iroh. On, it listens on every interface
and the QR code carries the LAN address. The macOS app needs the same setting.

## What to build next

1. **macOS desktop app.** The Windows app is a thin shell around the hub, so
   the same structure applies: a window, a menu bar icon in place of the tray,
   a LaunchAgent for start at sign-in, a `.app` bundle built locally, and a
   signed, notarised build for distribution. `agent_relay/desktop/app.py`
   exits on anything but Windows today.
2. **AntiGravity quota on macOS.** Find where AntiGravity keeps its sign-in on
   macOS (most likely the Keychain) and add it to `agy_cloud.py` beside the
   Windows Credential Manager read. The rest of that module is platform
   neutral.
3. **iOS and iPadOS app**, pairing through the QR code and following
   `docs/mobile-api-spec.json`.
4. **Android app.**

## Rules for this repository

- It is public. Nothing personal, no credentials, no hostnames, IP addresses
  or local folder paths of a real machine, and no screenshots of a real
  desktop. Test fixtures use placeholders such as `user@example.com`.
- Commits use the GitHub noreply address.
- Run `ruff check .` and `pytest tests -q` before every commit. CI runs both,
  plus a gitleaks secret scan, on every push.
- A `v*` tag publishes to PyPI through `.github/workflows/release.yml`. The
  Windows zip is built locally and attached to the GitHub release.
