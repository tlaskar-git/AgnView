"""What a phone on iroh may ask the hub to run.

A dispatch that arrives over iroh can reach only the named agents and the
enabled adapters, and only in a real local folder. The generic shell runner
that a LAN dispatch can reach for an unknown agent name is never reachable from
iroh. The LAN is unchanged.
"""

import os
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
