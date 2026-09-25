"""PATH repair for apps started from Finder, the Dock or a LaunchAgent.

Such apps inherit a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin), so agent
CLIs installed by npm, bun, Homebrew or a version manager are not found and
every chat reports "not installed". At start the macOS app asks the login
shell for its PATH, adds the well-known install directories and merges both
into os.environ, so every child process inherits the result.

Every function here is pure or takes its inputs as arguments, so the tests
run on any platform with no real shell.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

logger = logging.getLogger("agnview.cli_path")

SENTINEL_START = "__AGNVIEW_PATH_START__"
SENTINEL_END = "__AGNVIEW_PATH_END__"
BINS_START = "__AGNVIEW_BINS_START__"
BINS_END = "__AGNVIEW_BINS_END__"
# One shell call. A version manager such as nvm can take several seconds to
# load from .zshrc, so the old 5 second limit failed silently for those users.
SHELL_TIMEOUT_SECONDS = 10
# The whole repair, over every shell and mode, must not hold the app start.
REPAIR_BUDGET_SECONDS = 20
# Extra wait for a shell that was killed but whose output pipe stays open.
HANG_GRACE_SECONDS = 3
# A missing CLI triggers a repair retry, at most this often.
RETRY_INTERVAL_SECONDS = 30

# Login plus interactive reads .zprofile and .zshrc. Login alone reads
# .zprofile only. PATH is often set in one or the other, so both are asked.
SHELL_MODES = (("-l", "-i", "-c"), ("-l", "-c"))

# CLI names the app looks for.
CLI_NAMES = ("claude", "codex", "agy", "antigravity", "gemini", "node")

# Relative to the home directory.
HOME_DIRS = (
    ".local/bin",
    ".claude/local",
    ".npm-global/bin",
    ".npm/bin",
    ".bun/bin",
    ".volta/bin",
    ".cargo/bin",
    ".asdf/shims",
    ".local/share/mise/shims",
    ".local/share/fnm/aliases/default/bin",
    ".local/share/pnpm",
    "Library/pnpm",
    ".yarn/bin",
    ".config/yarn/global/node_modules/.bin",
    ".nix-profile/bin",
    ".antigravity/antigravity/bin",
    ".codex/bin",
    "bin",
)
SYSTEM_DIRS = (
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/Applications/Codex.app/Contents/Resources",
    "/Applications/Antigravity.app/Contents/Resources/app/bin",
)
# Home-relative folders that hold one directory per node version.
NODE_VERSION_ROOTS = (
    (".nvm/versions/node", "bin"),
    (".local/share/fnm/node-versions", "installation/bin"),
    ("Library/Application Support/fnm/node-versions", "installation/bin"),
    (".local/share/mise/installs/node", "bin"),
    (".asdf/installs/nodejs", "bin"),
)


def _split(path: str) -> List[str]:
    return [p for p in (path or "").split(os.pathsep) if p]


def merge_paths(existing: str, *additions: Iterable[str]) -> str:
    """Existing entries first, then the additions, with no duplicates."""
    seen = set()
    merged: List[str] = []
    for entry in _split(existing):
        if entry not in seen:
            seen.add(entry)
            merged.append(entry)
    for group in additions:
        for entry in group:
            if entry and entry not in seen:
                seen.add(entry)
                merged.append(entry)
    return os.pathsep.join(merged)


def parse_shell_path(output: str) -> List[str]:
    """The PATH printed between the sentinels. Noise around them is ignored."""
    if not output:
        return []
    start = output.rfind(SENTINEL_START)
    if start < 0:
        return []
    start += len(SENTINEL_START)
    end = output.find(SENTINEL_END, start)
    if end < 0:
        return []
    return _split(output[start:end].strip())


def _run_bounded(run: Callable, cmd: List[str], timeout: float, **kwargs):
    """run(cmd, timeout=...) that cannot hang the app start.

    subprocess.run kills a timed out shell, then waits for its output pipe to
    close. A background process started by .zshrc keeps that pipe open, so the
    wait never ends. Running the call in a daemon thread and giving up on it
    keeps the start-up bounded.
    """
    box: dict = {}

    def target():
        try:
            box["result"] = run(cmd, timeout=timeout, **kwargs)
        except BaseException as exc:  # handed back to the caller below
            box["error"] = exc

    worker = threading.Thread(target=target, name="agnview-path-probe", daemon=True)
    worker.start()
    worker.join(timeout + HANG_GRACE_SECONDS)
    if worker.is_alive():
        raise subprocess.TimeoutExpired(cmd, timeout)
    if "error" in box:
        raise box["error"]
    return box["result"]


def parse_shell_bins(output: str) -> List[str]:
    """Directories of the CLIs the shell resolved, from the block between the BINS sentinels."""
    if not output:
        return []
    start = output.rfind(BINS_START)
    if start < 0:
        return []
    start += len(BINS_START)
    end = output.find(BINS_END, start)
    if end < 0:
        return []
    dirs: List[str] = []
    for line in output[start:end].splitlines():
        line = line.strip()
        # Aliases and functions print as words, not paths. Only absolute paths count.
        if line.startswith("/") and "\0" not in line:
            parent = os.path.dirname(line)
            if parent and parent not in dirs:
                dirs.append(parent)
    return dirs


def shell_candidates(environ: Optional[Dict[str, str]] = None, account_shell: Optional[str] = None) -> List[str]:
    """$SHELL, then the account database shell, then zsh and bash.

    An app started from Finder has no $SHELL, so the account database is the
    reliable source of the user's own shell.
    """
    environ = os.environ if environ is None else environ
    if account_shell is None:
        try:
            import pwd

            account_shell = pwd.getpwuid(os.getuid()).pw_shell
        except Exception:
            account_shell = ""
    out: List[str] = []
    for shell in (environ.get("SHELL", ""), account_shell, "/bin/zsh", "/bin/bash"):
        if shell and shell not in out:
            out.append(shell)
    return out


@dataclass
class ShellProbe:
    """What the login shell gave back, kept so a failure leaves a trace."""

    shell: str = ""
    ok: bool = False
    path: List[str] = field(default_factory=list)
    bin_dirs: List[str] = field(default_factory=list)
    attempts: int = 0
    failures: List[str] = field(default_factory=list)


def probe_login_shell(
    shells: Optional[Iterable[str]] = None,
    run: Callable = subprocess.run,
    budget: float = REPAIR_BUDGET_SECONDS,
) -> ShellProbe:
    """Ask the login shell for its PATH and where it finds the CLIs. Never raises."""
    candidates = shell_candidates() if shells is None else list(shells)
    # The CLI lookup catches a CLI whose folder is set up in a way this module
    # does not know. It resolves names through the real shell, so it follows
    # whatever the user's own rc files do.
    names = " ".join(CLI_NAMES)
    command = (
        f'printf "%s%s%s" "{SENTINEL_START}" "$PATH" "{SENTINEL_END}"; '
        f'printf "%s\\n" "{BINS_START}"; '
        f"for c in {names}; do command -v \"$c\" 2>/dev/null; done; "
        f'printf "%s\\n" "{BINS_END}"'
    )
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env["AGNVIEW_PATH_PROBE"] = "1"
    extra = {"start_new_session": True} if os.name == "posix" else {}
    probe = ShellProbe()
    deadline = time.monotonic() + budget
    tried = set()
    for shell in candidates:
        if not shell or shell in tried:
            continue
        tried.add(shell)
        merged_path: List[str] = []
        merged_bins: List[str] = []
        for mode in SHELL_MODES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                probe.failures.append(f"{shell} {' '.join(mode)}: repair budget used up")
                break
            probe.attempts += 1
            try:
                result = _run_bounded(
                    run,
                    [shell, *mode, command],
                    min(SHELL_TIMEOUT_SECONDS, remaining),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    errors="replace",
                    env=env,
                    **extra,
                )
                out = getattr(result, "stdout", "") or ""
                found = parse_shell_path(out)
                if found:
                    merged_path = _split(merge_paths(os.pathsep.join(merged_path), found))
                    merged_bins = _split(merge_paths(os.pathsep.join(merged_bins), parse_shell_bins(out)))
                else:
                    probe.failures.append(f"{shell} {' '.join(mode)}: no PATH in output")
            except Exception as exc:
                probe.failures.append(f"{shell} {' '.join(mode)}: {type(exc).__name__}")
                logger.debug("Login shell %s gave no PATH", shell, exc_info=True)
        if merged_path:
            probe.shell = shell
            probe.ok = True
            probe.path = merged_path
            probe.bin_dirs = merged_bins
            return probe
    return probe


def login_shell_path(
    shells: Optional[Iterable[str]] = None,
    run: Callable = subprocess.run,
) -> List[str]:
    """Ask the login shell for its PATH. Never raises. Empty on failure."""
    return probe_login_shell(shells, run).path


def newest_nvm_bin(home: Path) -> Optional[str]:
    root = home / ".nvm" / "versions" / "node"
    try:
        versions = [d for d in root.iterdir() if (d / "bin").is_dir()]
    except OSError:
        return None
    if not versions:
        return None

    def key(d: Path):
        parts = []
        for piece in d.name.lstrip("v").split("."):
            parts.append(int(piece) if piece.isdigit() else 0)
        return parts

    return str(max(versions, key=key) / "bin")


def node_version_bins(home: Path) -> List[str]:
    """The bin folder of every installed node version, newest first."""
    found: List[str] = []
    for root_rel, bin_rel in NODE_VERSION_ROOTS:
        root = home / root_rel
        try:
            versions = [d for d in root.iterdir() if (d / bin_rel).is_dir()]
        except OSError:
            continue

        def key(d: Path):
            return [int(p) if p.isdigit() else 0 for p in d.name.lstrip("v").split(".")]

        for d in sorted(versions, key=key, reverse=True):
            found.append(str(d / bin_rel))
    return found


def known_dirs(home: Optional[Path] = None) -> List[str]:
    """Well-known install directories that exist on this machine."""
    home = home or Path.home()
    found = [str(home / rel) for rel in HOME_DIRS if os.path.isdir(home / rel)]
    try:
        found.extend(node_version_bins(home))
    except Exception:
        logger.debug("Could not list node versions", exc_info=True)
    found.extend(d for d in SYSTEM_DIRS if os.path.isdir(d))
    return found


def always_dirs(home: Optional[Path] = None) -> List[str]:
    """Folders added whether or not they exist yet.

    This list needs no file system check and no shell, so nothing in a Finder
    launch can make it come out empty. A folder that does not exist is
    harmless on a PATH.
    """
    home = home or Path.home()
    return [str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin"]


def system_path_dirs(etc: Path = Path("/etc")) -> List[str]:
    """What path_helper adds for a login shell: /etc/paths and /etc/paths.d/*."""
    files = [etc / "paths"]
    try:
        files.extend(sorted((etc / "paths.d").iterdir()))
    except OSError:
        pass
    out: List[str] = []
    for f in files:
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line.startswith("/") and line not in out:
                    out.append(line)
        except OSError:
            continue
    return out


def repaired_path(
    current: str,
    home: Optional[Path] = None,
    shells: Optional[Iterable[str]] = None,
    run: Callable = subprocess.run,
    probe: Optional[ShellProbe] = None,
) -> str:
    # Each source is guarded on its own, so one failure never discards the
    # others. The old code held all of them in one try block, so a single
    # error left the PATH untouched and left no trace but a log line.
    if probe is None:
        try:
            probe = probe_login_shell(shells, run)
        except Exception:
            logger.exception("The login shell probe failed and was skipped")
            probe = ShellProbe()
    groups: List[List[str]] = []
    for source in (
        lambda: probe.path,
        lambda: probe.bin_dirs,
        system_path_dirs,
        lambda: known_dirs(home),
        lambda: always_dirs(home),
    ):
        try:
            groups.append(list(source()))
        except Exception:
            logger.exception("A PATH source failed and was skipped")
    return merge_paths(current, *groups)


def _tilde(text: str, home: Optional[Path] = None) -> str:
    """Show the home directory as ~, so a log or message never carries a user name."""
    home_text = str(home or Path.home())
    if home_text and home_text not in ("/", "\\") and text:
        text = text.replace(home_text, "~")
        if "~" in text:
            text = text.replace("~\\", "~/")
    return text


@dataclass
class RepairReport:
    """The outcome of the last PATH repair, for the log and the diagnostics endpoint."""

    ran: bool = False
    shell: str = ""
    shell_ok: bool = False
    attempts: int = 0
    added: int = 0
    failures: List[str] = field(default_factory=list)
    path: str = ""
    # The PATH the process started with, before the repair.
    before: str = ""

    def as_dict(self, home: Optional[Path] = None) -> dict:
        data = asdict(self)
        data["before"] = _tilde(self.before, home)
        data["shell"] = _tilde(self.shell, home)
        data["failures"] = [_tilde(f, home) for f in self.failures]
        data["path"] = _tilde(self.path, home)
        return data

    def summary(self, home: Optional[Path] = None) -> str:
        if not self.ran:
            return "PATH repair did not run"
        return (
            f"PATH repair: shell={_tilde(self.shell, home) or 'none'} shell_ok={self.shell_ok} "
            f"attempts={self.attempts} added={self.added} PATH={_tilde(self.path, home)}"
        )


LAST_REPORT = RepairReport()
_last_repair_at: Optional[float] = None


def repair_environment(home: Optional[Path] = None, shells=None, run: Callable = subprocess.run) -> str:
    """Repair os.environ["PATH"] in place. Never raises. Logs one line."""
    global LAST_REPORT, _last_repair_at
    _last_repair_at = time.monotonic()
    try:
        current = os.environ.get("PATH", "")
        try:
            probe = probe_login_shell(shells, run)
        except Exception:
            logger.exception("The login shell probe failed and was skipped")
            probe = ShellProbe(failures=["probe raised"])
        fixed = repaired_path(current, home, shells, run, probe=probe)
        os.environ["PATH"] = fixed
        LAST_REPORT = RepairReport(
            ran=True,
            shell=probe.shell,
            shell_ok=probe.ok,
            attempts=probe.attempts,
            added=len(_split(fixed)) - len(_split(current)),
            failures=list(probe.failures),
            path=fixed,
            before=current,
        )
        logger.info("%s", LAST_REPORT.summary(home))
        for failure in probe.failures:
            logger.info("PATH repair shell note: %s", _tilde(failure, home))
    except Exception:
        logger.exception("Could not repair PATH")
    return os.environ.get("PATH", "")


def searched_dirs(home: Optional[Path] = None, path: Optional[str] = None) -> List[str]:
    """PATH entries, home shown as ~, so a message never carries a user name."""
    home_text = str(home or Path.home())
    out: List[str] = []
    for entry in _split(os.environ.get("PATH", "") if path is None else path):
        if home_text and (entry == home_text or entry.startswith(home_text + os.sep)):
            entry = "~" + entry[len(home_text):]
        out.append(entry.replace("\\", "/") if entry.startswith("~") else entry)
    return out


def which_any(
    *names: str,
    home: Optional[Path] = None,
    platform: Optional[str] = None,
    path: Optional[str] = None,
    repair: Optional[Callable[[], object]] = None,
) -> Optional[str]:
    """First of the names found. On macOS it repairs the PATH again on a miss.

    Other platforms behave exactly like shutil.which. On macOS a miss means the
    start-up repair may have failed (a slow shell, a timeout), so the repair
    runs again at most every RETRY_INTERVAL_SECONDS, then the well-known
    folders are searched directly. The result is a full path, so it works even
    if the PATH never gets fixed.
    """
    for name in names:
        found = shutil.which(name, path=path)
        if found:
            return found
    if (platform or sys.platform) != "darwin" or path is not None:
        return None
    now = time.monotonic()
    if _last_repair_at is None or now - _last_repair_at >= RETRY_INTERVAL_SECONDS:
        (repair or repair_environment)()
        for name in names:
            found = shutil.which(name)
            if found:
                return found
    direct = os.pathsep.join(known_dirs(home))
    for name in names:
        found = shutil.which(name, path=direct)
        if found:
            return found
    return None


def child_env(env: Dict[str, str], platform: Optional[str] = None, home: Optional[Path] = None) -> Dict[str, str]:
    """The PATH a spawned agent gets. On macOS it also holds the folders of node.

    An npm shim starts with #!/usr/bin/env node, so the folder of node must be
    on the PATH the child sees, not only the folder of the shim.
    """
    if (platform or sys.platform) == "darwin":
        env["PATH"] = merge_paths(env.get("PATH", ""), known_dirs(home))
    return env


def diagnostics(home: Optional[Path] = None, platform: Optional[str] = None) -> dict:
    """Read-only PATH facts for GET /api/diagnostics/path. Home is shown as ~."""
    lookup = {}
    for name in CLI_NAMES:
        found = which_any(name, name + ".cmd", name + ".exe", home=home, platform=platform)
        lookup[name] = {"found": bool(found), "path": _tilde(found, home) if found else None}
    try:
        from agent_relay import __version__ as version
    except Exception:
        version = "unknown"
    return {
        "version": version,
        "frozen": bool(getattr(sys, "frozen", False)),
        "pid": os.getpid(),
        "platform": platform or sys.platform,
        "path": searched_dirs(home),
        "repair": LAST_REPORT.as_dict(home),
        "lookup": lookup,
    }


def searched_note(platform: Optional[str] = None, home: Optional[Path] = None, path: Optional[str] = None) -> str:
    """Extra text for the not installed message. Empty except on macOS."""
    if (platform or sys.platform) != "darwin":
        return ""
    dirs = searched_dirs(home, path)
    if LAST_REPORT.ran:
        shell = (
            f"The login shell {_tilde(LAST_REPORT.shell, home)} was read."
            if LAST_REPORT.shell_ok
            else "No login shell gave its PATH."
        )
    else:
        shell = "The PATH repair did not run."
    note = (
        " AgnView searched: " + ", ".join(dirs) + ". " + shell + " "
        "If the command works in Terminal but not here, its folder is missing from that list. "
        "Starting AgnView from Terminal is a workaround, not a fix."
    )
    logger.warning("CLI not found. Searched: %s. %s", ", ".join(dirs), shell)
    return note
