"""What a phone on iroh may ask the hub to run.

A dispatch that arrives over iroh can reach only the named agents and the
enabled adapters, and only in a real local folder. The generic shell runner
that a LAN dispatch can reach for an unknown agent name is never reachable from
iroh. The LAN is unchanged.
"""

import os
import re
from typing import Any, Iterable, List, Optional

from .iroh_api import IROH_CLIENT_ADDRESS

ERROR_UNKNOWN_AGENT = "unknown_agent"
ERROR_BAD_DIRECTORY = "invalid_working_directory"

# The built-in dispatch targets the runner knows by name.
BUILTIN_AGENTS = frozenset({
    "claude", "claude_code", "codex", "chatgpt", "antigravity", "agy",
    "custom", "ollama", "local", "deepseek", "all",
})


class DispatchRefused(Exception):
    """A dispatch the hub refuses, with the detail string sent back."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def is_iroh_request(request: Any) -> bool:
    """True when the request was handed in by the iroh transport."""
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    port = getattr(client, "port", None)
    return (host, port) == IROH_CLIENT_ADDRESS


def check_agent(agent: str, adapter_manager: Any) -> None:
    """Allow a named built-in agent or an enabled adapter, nothing else."""
    target = (agent or "").lower().strip()
    if target in BUILTIN_AGENTS:
        return
    adapter = adapter_manager.get_adapter(target) if adapter_manager is not None else None
    if adapter is not None and getattr(adapter, "enabled", False):
        return
    raise DispatchRefused(ERROR_UNKNOWN_AGENT)


def _has_network_or_device_prefix(path: str) -> bool:
    flat = path.replace("/", "\\")
    return flat.startswith("\\\\")


def _inside(path: str, root: str) -> bool:
    path_key, root_key = os.path.normcase(path), os.path.normcase(root)
    try:
        return os.path.commonpath([path_key, root_key]) == root_key
    except ValueError:
        # Different drives.
        return False


def check_working_directory(working_directory: Optional[str], roots: Iterable[str]) -> Optional[str]:
    """Return the resolved folder to run in, or raise DispatchRefused.

    None means the caller gave no folder. With no roots configured the runner
    picks its own default. With roots, the first root is the default.
    """
    root_list: List[str] = [os.path.realpath(r) for r in roots]

    if working_directory is None:
        return root_list[0] if root_list else None
    if not isinstance(working_directory, str) or not working_directory.strip():
        raise DispatchRefused(ERROR_BAD_DIRECTORY)
    if "\x00" in working_directory or _has_network_or_device_prefix(working_directory):
        raise DispatchRefused(ERROR_BAD_DIRECTORY)

    try:
        resolved = os.path.realpath(working_directory)
    except (OSError, ValueError):
        raise DispatchRefused(ERROR_BAD_DIRECTORY)
    if _has_network_or_device_prefix(resolved) or not os.path.isdir(resolved):
        raise DispatchRefused(ERROR_BAD_DIRECTORY)
    if root_list and not any(_inside(resolved, root) for root in root_list):
        raise DispatchRefused(ERROR_BAD_DIRECTORY)
    return resolved


ERROR_FORBIDDEN_FILE = "forbidden_file"

# What GET /api/system/files never lists: hidden files and folders, tool and
# build folders, and these file types. A phone on iroh can attach a project
# file only when the listing would have shown it.
LISTING_IGNORED_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache",
    ".idea", ".vscode", "dist", "build",
})
LISTING_SKIPPED_SUFFIXES = (".pyc", ".log", ".png", ".jpg", ".jpeg", ".db")

_UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
MAX_FILE_ENTRY_CHARS = 4096


def listing_shows_dir(name: str) -> bool:
    return name not in LISTING_IGNORED_DIRS and not name.startswith(".")


def listing_shows_file(name: str) -> bool:
    return not name.startswith(".") and not name.endswith(LISTING_SKIPPED_SUFFIXES)


def _relative_parts(path: str, base: str) -> Optional[List[str]]:
    """The parts of path under base, or None when path is not inside base."""
    if not _inside(path, base):
        return None
    rel = os.path.relpath(path, base)
    return [] if rel == "." else rel.split(os.sep)


def _check_one_file(
    entry: Any,
    working_directory: Optional[str],
    roots: List[str],
    uploads_root: Optional[str],
) -> str:
    if not isinstance(entry, str) or not entry or len(entry) > MAX_FILE_ENTRY_CHARS:
        raise DispatchRefused(ERROR_FORBIDDEN_FILE)
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in entry) or _has_network_or_device_prefix(entry):
        raise DispatchRefused(ERROR_FORBIDDEN_FILE)

    if os.path.isabs(entry):
        candidate = entry
    elif working_directory:
        candidate = os.path.join(working_directory, entry)
    else:
        raise DispatchRefused(ERROR_FORBIDDEN_FILE)
    try:
        real = os.path.realpath(candidate)
    except (OSError, ValueError):
        raise DispatchRefused(ERROR_FORBIDDEN_FILE)
    if _has_network_or_device_prefix(real):
        raise DispatchRefused(ERROR_FORBIDDEN_FILE)

    # Anything under the uploads folder is judged by the uploads rule alone:
    # <folder>/<32 hex id>/<a visible file name>, a real file. The hub's own
    # bookkeeping files are hidden and so never match.
    if uploads_root:
        upload_parts = _relative_parts(real, os.path.realpath(uploads_root))
        if upload_parts is not None:
            if (
                len(upload_parts) == 2
                and _UPLOAD_ID.fullmatch(upload_parts[0])
                and not upload_parts[1].startswith(".")
                and os.path.isfile(real)
            ):
                return real
            raise DispatchRefused(ERROR_FORBIDDEN_FILE)

    bases = [os.path.realpath(working_directory)] if working_directory else [os.path.realpath(r) for r in roots]
    for base in bases:
        parts = _relative_parts(real, base)
        if not parts:
            continue
        if all(listing_shows_dir(p) for p in parts[:-1]) and listing_shows_file(parts[-1]) and os.path.isfile(real):
            return real
    raise DispatchRefused(ERROR_FORBIDDEN_FILE)


def check_files(
    files: Optional[List[str]],
    working_directory: Optional[str],
    roots: Iterable[str],
    uploads_root: Optional[str],
) -> Optional[List[str]]:
    """Return the files as absolute real paths, or raise DispatchRefused.

    Each file must be an upload the hub stored, or a file the project listing
    would show, inside the folder the agent runs in (or inside a configured
    dispatch root when there is no folder, as for a pipeline task).
    """
    if files is None:
        return None
    if not isinstance(files, list):
        raise DispatchRefused(ERROR_FORBIDDEN_FILE)
    root_list = list(roots)
    return [_check_one_file(entry, working_directory, root_list, uploads_root) for entry in files]
