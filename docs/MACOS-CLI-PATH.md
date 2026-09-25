# Agent CLIs and the PATH on macOS

An app started from Finder, the Dock or a login item gets a minimal PATH
(`/usr/bin:/bin:/usr/sbin:/sbin`). Claude Code, Codex and AntiGravity usually
sit in `~/.local/bin`, a Homebrew folder or a node version folder, so AgnView
repairs its own PATH at start.

## What the repair does

1. Asks the user's login shell for its PATH and for where it finds `claude`,
   `codex`, `agy`, `antigravity`, `gemini` and `node`. It tries `-l -i` and `-l`,
   because PATH is set in `.zprofile` for some people and `.zshrc` for others.
   The shell comes from `$SHELL`, then the account database, then `/bin/zsh`
   and `/bin/bash`. A slow or hung shell is abandoned after 10 seconds.
2. Adds `/etc/paths` and `/etc/paths.d`.
3. Adds known folders that exist: `~/.local/bin`, npm, bun, volta, cargo, asdf,
   mise, pnpm, yarn, every nvm, fnm, mise and asdf node version, Homebrew and
   the CLI folders inside the Codex and AntiGravity apps.
4. Always adds `~/.local/bin`, `/opt/homebrew/bin` and `/usr/local/bin`, with no
   shell and no file check, so this step cannot come out empty.

Each source is guarded on its own. One failure never discards the others.

When a chat cannot find a CLI, AgnView repairs the PATH again (at most every 30
seconds) and searches the known folders directly. Agents start with the folders
of node on their PATH, because an npm command starts with `env node`.

## Reading a failure

The log (`~/.agnview/logs/desktop.log`) has one line per start:

    PATH repair: shell=/bin/zsh shell_ok=True attempts=2 added=6 PATH=...

The home folder shows as `~`. A missing CLI adds a warning with the folders that
were searched, and the same list appears in the chat message.

`GET /api/diagnostics/path` returns the PATH the hub sees, the outcome of the
repair (including the PATH the app started with) and where `claude`, `codex`,
`agy`, `antigravity`, `gemini` and `node` resolve. It follows the same access
rules as every other `/api` route. The home folder shows as `~`.

## CI check

`tools/macos/finder_launch_check.sh` launches the built app through
LaunchServices with the minimal PATH, with fake CLIs in `~/.local/bin`, and
fails the macOS job if the diagnostics endpoint does not resolve them or a chat
reports them missing.
