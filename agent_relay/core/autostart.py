"""Autostart-on-login support for AgnView, for Windows, macOS and Linux.

Each platform gets its own enable/disable/status implementation using only
the OS-native mechanism (no third-party dependency):

- Windows: a value under HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\
  CurrentVersion\\Run, via the stdlib winreg module.
- macOS: a LaunchAgent plist under ~/Library/LaunchAgents.
- Linux: an XDG autostart desktop entry under ~/.config/autostart.

The command registered always prefers the `agnview` console script if it is
on PATH, and only falls back to `<python> -m agent_relay.cli.main serve` if
it is not. That keeps the registered command working regardless of whether
AgnView was installed with pip, pipx, or `uv tool install`.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

APP_NAME = "AgnView"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
MACOS_LABEL = "com.agnview.hub"
MACOS_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
LINUX_AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "agnview.desktop"


def _resolve_command_parts() -> list[str]:
    """Work out the command that should run AgnView at login.

    Prefers the `agnview` console script on PATH (works for both `pip
    install` and `uv tool install`), and falls back to running the module
    with the current interpreter otherwise.
    """
    agnview_path = shutil.which("agnview")
    if agnview_path:
        return [agnview_path, "serve"]
    return [sys.executable, "-m", "agent_relay.cli.main", "serve"]


def _resolve_command_string() -> str:
    """Same as _resolve_command_parts, but as a single quoted string for
    registries and config files that take one command line (Windows Run
    key, XDG desktop entry Exec=)."""
    parts = _resolve_command_parts()
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


# --- Windows ---------------------------------------------------------------

def _windows_enable() -> str:
    if sys.platform != "win32":
        raise RuntimeError("Windows autostart requires the winreg module, which is only available on Windows.")
    import winreg

    command = _resolve_command_string()
    key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
    finally:
        winreg.CloseKey(key)
    return f"Autostart enabled. Registered under HKEY_CURRENT_USER\\{RUN_KEY_PATH}\\{APP_NAME} with command: {command}"


def _windows_disable() -> str:
    if sys.platform != "win32":
        raise RuntimeError("Windows autostart requires the winreg module, which is only available on Windows.")
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE)
    except FileNotFoundError:
        return "Autostart was already disabled (Run key does not exist)."
    try:
        try:
            winreg.DeleteValue(key, APP_NAME)
            return f"Autostart disabled. Removed HKEY_CURRENT_USER\\{RUN_KEY_PATH}\\{APP_NAME}."
        except FileNotFoundError:
            return "Autostart was already disabled (no AgnView value present)."
    finally:
        winreg.CloseKey(key)


def _windows_status() -> bool:
    if sys.platform != "win32":
        raise RuntimeError("Windows autostart requires the winreg module, which is only available on Windows.")
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return False
    try:
        try:
            winreg.QueryValueEx(key, APP_NAME)
            return True
        except FileNotFoundError:
            return False
    finally:
        winreg.CloseKey(key)


# --- macOS -------------------------------------------------------------------

def _macos_plist_contents() -> str:
    parts = _resolve_command_parts()
    args_xml = "\n".join(f"        <string>{p}</string>" for p in parts)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""


def _macos_enable() -> str:
    if sys.platform != "darwin":
        raise RuntimeError("macOS autostart can only be enabled on macOS.")
    import subprocess

    MACOS_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MACOS_PLIST_PATH.write_text(_macos_plist_contents())
    subprocess.run(["launchctl", "load", "-w", str(MACOS_PLIST_PATH)], check=False)
    return f"Autostart enabled. Wrote {MACOS_PLIST_PATH} and loaded it with launchctl."


def _macos_disable() -> str:
    if sys.platform != "darwin":
        raise RuntimeError("macOS autostart can only be disabled on macOS.")
    import subprocess

    if not MACOS_PLIST_PATH.exists():
        return "Autostart was already disabled (LaunchAgent plist does not exist)."
    subprocess.run(["launchctl", "unload", "-w", str(MACOS_PLIST_PATH)], check=False)
    MACOS_PLIST_PATH.unlink()
    return f"Autostart disabled. Unloaded and removed {MACOS_PLIST_PATH}."


def _macos_status() -> bool:
    if sys.platform != "darwin":
        raise RuntimeError("macOS autostart status can only be checked on macOS.")
    return MACOS_PLIST_PATH.exists()


# --- Linux -------------------------------------------------------------------

def _linux_desktop_entry_contents() -> str:
    exec_line = _resolve_command_string()
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={exec_line}
Hidden=false
X-GNOME-Autostart-enabled=true
"""


def _linux_enable() -> str:
    LINUX_AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINUX_AUTOSTART_PATH.write_text(_linux_desktop_entry_contents())
    return f"Autostart enabled. Wrote {LINUX_AUTOSTART_PATH}."


def _linux_disable() -> str:
    if not LINUX_AUTOSTART_PATH.exists():
        return "Autostart was already disabled (desktop entry does not exist)."
    LINUX_AUTOSTART_PATH.unlink()
    return f"Autostart disabled. Removed {LINUX_AUTOSTART_PATH}."


def _linux_status() -> bool:
    return LINUX_AUTOSTART_PATH.exists()


# --- Public, platform-dispatched API ---------------------------------------

def enable() -> str:
    """Register AgnView to start automatically at login. Returns a
    human-readable result string."""
    if sys.platform == "win32":
        return _windows_enable()
    if sys.platform == "darwin":
        return _macos_enable()
    return _linux_enable()


def disable() -> str:
    """Remove AgnView's autostart-on-login registration, if present.
    Returns a human-readable result string."""
    if sys.platform == "win32":
        return _windows_disable()
    if sys.platform == "darwin":
        return _macos_disable()
    return _linux_disable()


def status() -> bool:
    """Return whether AgnView is currently registered to start at login."""
    if sys.platform == "win32":
        return _windows_status()
    if sys.platform == "darwin":
        return _macos_status()
    return _linux_status()
