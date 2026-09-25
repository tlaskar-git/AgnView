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
import subprocess
from pathlib import Path
from typing import Callable, Iterable, List, Optional

logger = logging.getLogger("agnview.cli_path")

SENTINEL_START = "__AGNVIEW_PATH_START__"
SENTINEL_END = "__AGNVIEW_PATH_END__"
SHELL_TIMEOUT_SECONDS = 5

# Relative to the home directory.
HOME_DIRS = (
    ".local/bin",
    ".claude/local",
    ".npm-global/bin",
    ".bun/bin",
    ".volta/bin",
    ".cargo/bin",
    ".asdf/shims",
    ".local/share/fnm/aliases/default/bin",
)
SYSTEM_DIRS = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin")


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


def login_shell_path(
    shells: Optional[Iterable[str]] = None,
    run: Callable = subprocess.run,
) -> List[str]:
    """Ask the login shell for its PATH. Never raises. Empty on failure."""
    if shells is None:
        candidates = [os.environ.get("SHELL", ""), "/bin/zsh", "/bin/bash"]
    else:
        candidates = list(shells)
    command = f'printf "%s%s%s" "{SENTINEL_START}" "$PATH" "{SENTINEL_END}"'
    tried = set()
    for shell in candidates:
        if not shell or shell in tried:
            continue
        tried.add(shell)
        try:
            result = run(
                [shell, "-l", "-i", "-c", command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=SHELL_TIMEOUT_SECONDS,
                text=True,
                errors="replace",
            )
            found = parse_shell_path(getattr(result, "stdout", "") or "")
            if found:
                return found
        except Exception:
            logger.debug("Login shell %s gave no PATH", shell, exc_info=True)
    return []


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


def known_dirs(home: Optional[Path] = None) -> List[str]:
    """Well-known install directories that exist on this machine."""
    home = home or Path.home()
    found = [str(home / rel) for rel in HOME_DIRS if (home / rel).is_dir()]
    nvm = newest_nvm_bin(home)
    if nvm:
        found.append(nvm)
    found.extend(d for d in SYSTEM_DIRS if os.path.isdir(d))
    return found


def repaired_path(
    current: str,
    home: Optional[Path] = None,
    shells: Optional[Iterable[str]] = None,
    run: Callable = subprocess.run,
) -> str:
    return merge_paths(current, login_shell_path(shells, run), known_dirs(home))


def repair_environment(home: Optional[Path] = None, shells=None, run: Callable = subprocess.run) -> str:
    """Repair os.environ["PATH"] in place. Never raises."""
    try:
        os.environ["PATH"] = repaired_path(os.environ.get("PATH", ""), home, shells, run)
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


def searched_note(platform: Optional[str] = None, home: Optional[Path] = None, path: Optional[str] = None) -> str:
    """Extra text for the not installed message. Empty except on macOS."""
    import sys

    if (platform or sys.platform) != "darwin":
        return ""
    dirs = searched_dirs(home, path)
    return (
        " AgnView searched: " + ", ".join(dirs) + ". "
        "If the command works in Terminal but not here, its folder is missing from that list. "
        "Starting AgnView from Terminal is a workaround, not a fix."
    )
