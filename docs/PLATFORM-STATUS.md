# Platform status

What AgnView does on each platform today, what differs between them, and what
is still to build. Read this first when starting work on a platform that is
not Windows.

## Summary

| Platform | How it runs | Status |
|---|---|---|
| Windows | Desktop app (`AgnView.exe`), or `agnview serve` in a browser | Complete. Released as `AgnView-windows-x64.zip` on every GitHub release |
| macOS | Desktop app (`AgnView.app`), or `agnview serve` in a browser | Desktop app built and tested on every change by `.github/workflows/macos.yml`, and attached to each GitHub release from the next `v*` tag on as `AgnView-macos.dmg`. Signed ad hoc until the signing secrets are set |
| Linux | `agnview serve` in a browser | Works. No desktop app |
| iOS and iPadOS | Companion app that pairs with a hub | Not built. Design and spec only, in a separate repository. The hub side is ready: console, status, Usage, Pipelines, Sessions and dispatch over the LAN and over iroh |
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
- A newer copy takes over. The running copy records its process ID and
  version in `~/.agnview/desktop-instance.json`. Starting a newer copy closes
  an older running one and starts in its place. A copy from before records
  existed is found by its window titled AgnView. A same or newer running copy
  is brought forward instead. With Start with Windows off, a leftover sign-in
  task is removed. The macOS app does the same: it uses the same record, finds
  a copy without one by its bundle identifier, closes it with SIGTERM (SIGKILL
  after a timeout), and removes a leftover LaunchAgent when Start at login is
  off. Unit tests cover this. It is not yet checked on a real Mac.
- Every child process starts without a console window (`agent_relay/core/proc.py`).

## macOS desktop app

Code: `agent_relay/desktop/macos_app.py` (window and menu bar icon) and
`agent_relay/desktop/macos.py` (Start at login and single instance, no GUI).
The settings, the hub, the port and Allow phones on my network are the shared
code in `agent_relay/desktop/app.py`. Design: `docs/adr/ADR-MACOS-DESKTOP.md`.
Build: `tools/build-macos.sh` on a Mac, or `.github/workflows/macos.yml` on
every pull request, push to `main` and `v*` tag.

- A pywebview window on WebKit over the hub on loopback, and a menu bar icon
  with Open AgnView, Start at login, Allow phones on my network and Quit
  AgnView. The icon is a native NSStatusItem in pywebview's own Cocoa run
  loop. pystray is not used on macOS.
- The close button hides the window, and the menu bar icon brings it back.
  The Dock icon shows while the window is open and goes with it. Cmd+Q, Quit
  in the Dock and Quit AgnView in the menu stop the app and the hub.
- Start at login is a LaunchAgent, `~/Library/LaunchAgents/com.agnview.desktop.plist`,
  off by default, shared with the dashboard's autostart switch.
- Port 18845 by default, moving to the next free port when that one is busy.
- One instance per user: a lock file and a Unix socket in `~/.agnview`. A
  second launch brings the open window forward and exits.
- Agent chats run headless. Nothing opens Terminal.
- CI launches the built app on a macOS runner, waits for the hub on
  127.0.0.1:18845, checks a second launch hands over, and uploads screenshots
  of the window and the menu bar with the disk image.
- Signing and notarisation run in CI once the secrets in
  `docs/MACOS-SIGNING.md` exist. Until then `AgnView-macos.dmg` is signed ad
  hoc, and a Mac opens it only with right-click, Open.
- The CI build is for Apple silicon. Intel Macs use `agnview serve`.

## Usage tab sources

Every card needs no setup. Discovery (`agent_relay/core/usage/discover.py`)
adds a card for each signed-in tool at every start and every ten minutes, and
a card the person deletes stays deleted.

| Card | Source | Windows | macOS | Linux |
|---|---|---|---|---|
| Claude | Anthropic account usage API with the Claude Code sign-in. AgnView asks Claude Code to renew it before it expires | `~/.claude/.credentials.json` | Keychain, `Claude Code-credentials` | `~/.claude/.credentials.json` |
| ChatGPT | ChatGPT usage API with the Codex sign-in, `~/.codex/auth.json` | Yes | Yes | Yes |
| AntiGravity and Gemini | Google's quota service with AntiGravity's own sign-in, renewed in memory (`adapters/agy_cloud.py`) | Windows Credential Manager, `gemini:antigravity` | Keychain, service `gemini`, account `antigravity`, read with `security`. macOS asks once to allow the read. Untested against a real AntiGravity sign-in on a Mac | Not built. Falls back to reading the app's model picker while AntiGravity is open |

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

Over iroh the hub serves the live console and, in API mode, the mobile API a
phone needs away from the LAN: status, Usage, Pipelines (jobs and the
request-revision and fail task actions), Sessions and prompt dispatch. Each
call goes through the hub's own FastAPI app in-process, behind the same
pairing key and rate limit as the LAN, and only allowlisted routes answer.
`docs/PAIRING.md` section 5 has the protocol. `tools/iroh-client.py --api`
exercises it. Tested over real iroh endpoints on the loopback interface in
`tests/test_iroh_api_live.py`. Not yet tested from a phone through a public
relay. Turn API mode off with `iroh_api_enabled: false` in
`~/.agnview/config.yaml` or `AGNVIEW_IROH_API=0`.

Phone features the hub now serves, on the LAN and over iroh:

- **Uploads.** A phone can upload files, including files of several hundred
  megabytes, in resumable 1 MiB chunks, and attach the returned path to a chat
  prompt or a pipeline task. The LAN uses `PUT /api/uploads/{id}?offset=N`.
  Over iroh a new `upload_chunk` op carries one request line and then exactly
  `length` raw bytes. Files are stored in a private folder per upload, capped
  by size, total storage, free disk, concurrent uploads and age. The
  `uploads` capability in the hello frame says whether the hub accepts them
  over iroh. `docs/REMOTE-ACCESS.md` section 6 has the limits and the
  switches, `docs/PAIRING.md` section 5 the framing. Tested over real iroh
  endpoints in `tests/test_iroh_uploads_live.py`.
- **Per-task options.** A task in `POST /api/jobs` can carry `model`,
  `effort` and `files`. Model and effort are checked against the lists in
  `GET /api/system/capabilities`, and files follow the same rule as a
  dispatch. The task prompt, the task's API record and the MCP claim reply
  all carry them. The hub does not launch an agent for a pipeline task: the
  agent that claims the task reads the options. `POST /api/jobs` is not on the
  iroh allowlist, so a phone creates a job over the LAN only.

The Windows desktop app has an Allow phones on my network setting, in the
tray menu and on the pairing screen, off by default. Off, it listens on
loopback and the QR code's `lan` field is `127.0.0.1:<port>`, which a client
skips, so the phone goes straight to iroh. On, it listens on every interface
and the QR code carries the LAN address. The macOS app has the same setting,
in its menu bar menu and on the pairing screen.

## What to build next

1. **Sign and notarise the macOS app.** Add the secrets in
   `docs/MACOS-SIGNING.md`. The workflow then signs, notarises and staples
   with no code change.
2. **Confirm the macOS app on a real Mac**: the menu bar menu, Start at login
   across a log out and in, Allow phones on my network with a phone, and the
   AntiGravity card with a real AntiGravity sign-in.
3. **iOS and iPadOS app**, pairing through the QR code and following
   `docs/mobile-api-spec.json`, over the LAN or over iroh in API mode.
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
  `.github/workflows/macos.yml` attaches `AgnView-macos.dmg` to the same
  release, and creates the release first when it does not exist yet.
