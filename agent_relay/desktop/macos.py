"""Platform logic for the AgnView macOS desktop app that needs no GUI.

Everything here imports and runs on any platform, so the tests cover it on
Linux, macOS and Windows alike. The Cocoa window and menu bar icon live in
``macos_app.py``, which only macOS imports.

- Start at login is a per-user LaunchAgent in ~/Library/LaunchAgents. The
  file is written or removed and nothing else: launchd reads it at the next
  login. Loading it now would start a second copy at once, and unloading a
  running job would stop the app that asked.
- One instance per user is an exclusive lock on a file in ~/.agnview. A
  second launch that cannot take the lock sends "show" over a Unix socket
  beside it, so the first instance brings its window forward, then exits.
"""

from __future__ import annotations

import logging
import os
import plistlib
import socket
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence

logger = logging.getLogger("agnview.desktop")

BUNDLE_ID = "com.agnview.desktop"
LAUNCH_AGENT_LABEL = BUNDLE_ID
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
DATA_DIR = Path.home() / ".agnview"
LOCK_PATH = DATA_DIR / "desktop.lock"
SOCKET_PATH = DATA_DIR / "desktop.sock"
SHOW_MESSAGE = b"show\n"


# --- Start at login -------------------------------------------------------------

def launch_arguments() -> List[str]:
    """The command that starts AgnView at login, hidden behind its menu bar
    icon. A built app runs its own executable inside AgnView.app. A source
    install runs the module with the interpreter that is running now."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--minimized"]
    return [sys.executable, "-m", "agent_relay.desktop", "--minimized"]


def launch_agent_plist(arguments: Sequence[str], label: str = LAUNCH_AGENT_LABEL) -> bytes:
    """The LaunchAgent that starts AgnView once at login.

    LimitLoadToSessionType Aqua keeps it to a graphical login, where a window
    and a menu bar icon make sense. KeepAlive is off, so Quit AgnView stays
    quit until the next login.
    """
    return plistlib.dumps(
        {
            "Label": label,
            "ProgramArguments": [str(arg) for arg in arguments],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
            "LimitLoadToSessionType": "Aqua",
        }
    )


def registered_arguments(path: Optional[Path] = None) -> Optional[List[str]]:
    """The ProgramArguments of the saved LaunchAgent, or None without one."""
    path = path or LAUNCH_AGENT_PATH
    try:
        with open(path, "rb") as handle:
            data = plistlib.load(handle)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    arguments = data.get("ProgramArguments") if isinstance(data, dict) else None
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        return None
    return arguments


def login_item_registered(path: Optional[Path] = None) -> bool:
    """True when the LaunchAgent exists and starts this install."""
    return registered_arguments(path) == launch_arguments()


def set_login_item(enabled: bool, path: Optional[Path] = None) -> None:
    """Write or remove the LaunchAgent. Raises OSError when the folder is not
    writable, so the caller saves nothing."""
    path = path or LAUNCH_AGENT_PATH
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".tmp")
        partial.write_bytes(launch_agent_plist(launch_arguments()))
        os.replace(partial, path)
    else:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def remove_serve_launch_agent() -> None:
    """Remove the LaunchAgent `agnview serve` registers, so the browser-only
    hub does not start at login beside the desktop app. The same as the
    Windows app removing the old Run key value. A hub it started keeps
    running until it is stopped or the person logs out."""
    from ..core import autostart

    try:
        autostart.MACOS_PLIST_PATH.unlink()
    except FileNotFoundError:
        pass


# --- Single instance -------------------------------------------------------------

class SingleInstance:
    """One AgnView per user on macOS.

    The first instance holds an exclusive lock on ``lock_path`` for as long as
    it runs. The kernel drops the lock when the process ends, however it ends,
    so a crash never leaves a stale lock behind. A later instance sends "show"
    to ``socket_path`` and gives up.
    """

    def __init__(self, lock_path: Optional[Path] = None, socket_path: Optional[Path] = None):
        self.lock_path = Path(lock_path or LOCK_PATH)
        self.socket_path = Path(socket_path or SOCKET_PATH)
        self._lock_handle = None
        self._server: Optional[socket.socket] = None

    def claim(self) -> bool:
        """True for the first instance. A later one wakes the first and gets
        False."""
        import fcntl

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            self.wake_first()
            return False
        self._lock_handle = handle
        return True

    def wake_first(self) -> bool:
        """Ask the running instance to show its window. False when nothing
        answers."""
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2.0)
                client.connect(str(self.socket_path))
                client.sendall(SHOW_MESSAGE)
            return True
        except OSError:
            return False

    def listen(self, on_show: Callable[[], None]) -> None:
        """Call on_show for every "show" a later launch sends."""
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(4)
        self._server = server

        def loop():
            while True:
                try:
                    connection, _address = server.accept()
                except OSError:
                    return
                with connection:
                    connection.settimeout(2.0)
                    try:
                        message = connection.recv(64)
                    except OSError:
                        continue
                if message.startswith(SHOW_MESSAGE.strip()):
                    try:
                        on_show()
                    except Exception:
                        logger.exception("Could not show the window for a second launch")

        threading.Thread(target=loop, name="agnview-show-watch", daemon=True).start()

    def release(self) -> None:
        if self._server is not None:
            try:
                # Wakes the accept() in the watch thread, so it ends.
                self._server.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None
            try:
                self.socket_path.unlink()
            except OSError:
                pass
        if self._lock_handle is not None:
            self._lock_handle.close()
            self._lock_handle = None
