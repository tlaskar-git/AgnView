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

import re
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

APP_NAME = "AgnView"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
MACOS_LABEL = "com.agnview.hub"
MACOS_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"
LINUX_AUTOSTART_PATH = Path.home() / ".config" / "autostart" / "agnview.desktop"

# Marker that a person explicitly ran `agnview autostart disable`. Its
# presence stops `ensure_enabled_by_default` from turning autostart back on
# behind their back on the next `agnview serve`.
OPT_OUT_MARKER = Path.home() / ".agnview" / ".autostart_opt_out"
# Written by the Windows desktop app. Its presence means the desktop app is
# installed and owns starting at sign-in.
DESKTOP_SETTINGS = Path.home() / ".agnview" / "desktop.json"


def _resolve_command_parts(serve_args: Sequence[str] = ()) -> list[str]:
    """Work out the command that should run AgnView at login.

    Prefers the `agnview` console script on PATH (works for both `pip
    install` and `uv tool install`), and falls back to running the module
    with the current interpreter otherwise.

    serve_args carries the options the hub was last started with, such as
    --listen-lan or --port. Without them the hub came back after a reboot
    bound to loopback on the default port, so a paired phone lost it with no
    error anywhere. Nothing secret belongs in here: these end up in a registry
    value, a plist and a desktop entry.
    """
    agnview_path = shutil.which("agnview")
    if agnview_path:
        parts = [agnview_path, "serve"]
    else:
        parts = [sys.executable, "-m", "agent_relay.cli.main", "serve"]
    return parts + [str(arg) for arg in serve_args]


def _resolve_command_string(serve_args: Sequence[str] = ()) -> str:
    """Same as _resolve_command_parts, but as a single quoted string for
    registries and config files that take one command line (Windows Run
    key, XDG desktop entry Exec=)."""
    parts = _resolve_command_parts(serve_args)
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


# --- Windows ---------------------------------------------------------------

def _windows_enable(serve_args: Sequence[str] = ()) -> str:
    if sys.platform != "win32":
        raise RuntimeError("Windows autostart requires the winreg module, which is only available on Windows.")
    import winreg

    command = _resolve_command_string(serve_args)
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


def _windows_registered_command() -> Optional[str]:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ)
    except FileNotFoundError:
        return None
    try:
        try:
            value, _kind = winreg.QueryValueEx(key, APP_NAME)
            return value
        except FileNotFoundError:
            return None
    finally:
        winreg.CloseKey(key)


# --- macOS -------------------------------------------------------------------

def _macos_plist_contents(serve_args: Sequence[str] = ()) -> str:
    parts = _resolve_command_parts(serve_args)
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


def _macos_enable(serve_args: Sequence[str] = ()) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("macOS autostart can only be enabled on macOS.")
    import subprocess

    MACOS_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MACOS_PLIST_PATH.write_text(_macos_plist_contents(serve_args))
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


def _macos_registered_command() -> Optional[str]:
    """Rebuild the registered command line from the plist's ProgramArguments,
    so it can be compared with what this hub would register now."""
    if not MACOS_PLIST_PATH.exists():
        return None
    try:
        contents = MACOS_PLIST_PATH.read_text()
    except OSError:
        return None

    array = contents.split("<array>", 1)
    if len(array) < 2:
        return None
    body = array[1].split("</array>", 1)[0]
    parts = re.findall(r"<string>(.*?)</string>", body, flags=re.DOTALL)
    if not parts:
        return None
    return " ".join(f'"{p}"' if " " in p else p for p in parts)


# --- Linux -------------------------------------------------------------------

def _linux_desktop_entry_contents(serve_args: Sequence[str] = ()) -> str:
    exec_line = _resolve_command_string(serve_args)
    return f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={exec_line}
Hidden=false
X-GNOME-Autostart-enabled=true
"""


def _linux_enable(serve_args: Sequence[str] = ()) -> str:
    LINUX_AUTOSTART_PATH.parent.mkdir(parents=True, exist_ok=True)
    LINUX_AUTOSTART_PATH.write_text(_linux_desktop_entry_contents(serve_args))
    return f"Autostart enabled. Wrote {LINUX_AUTOSTART_PATH}."


def _linux_disable() -> str:
    if not LINUX_AUTOSTART_PATH.exists():
        return "Autostart was already disabled (desktop entry does not exist)."
    LINUX_AUTOSTART_PATH.unlink()
    return f"Autostart disabled. Removed {LINUX_AUTOSTART_PATH}."


def _linux_status() -> bool:
    return LINUX_AUTOSTART_PATH.exists()


def _linux_registered_command() -> Optional[str]:
    if not LINUX_AUTOSTART_PATH.exists():
        return None
    try:
        for line in LINUX_AUTOSTART_PATH.read_text().splitlines():
            if line.startswith("Exec="):
                return line[len("Exec="):].strip()
    except OSError:
        return None
    return None


# --- Public, platform-dispatched API ---------------------------------------

def enable(serve_args: Sequence[str] = ()) -> str:
    """Register AgnView to start automatically at login, with the options the
    hub is serving under. Returns a human-readable result string."""
    if sys.platform == "win32":
        return _windows_enable(serve_args)
    if sys.platform == "darwin":
        return _macos_enable(serve_args)
    return _linux_enable(serve_args)


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


def registered_command() -> Optional[str]:
    """Return the command registered to run at login, or None.

    Used to notice that the registration no longer matches how the hub is
    actually being served, for example after somebody starts serving with
    --listen-lan for the first time.
    """
    if sys.platform == "win32":
        return _windows_registered_command()
    if sys.platform == "darwin":
        return _macos_registered_command()
    return _linux_registered_command()


def enable_and_clear_opt_out(serve_args: Sequence[str] = ()) -> str:
    """Enable autostart and forget any earlier explicit opt-out, so a
    person who disables it and later re-enables it gets the default
    behaviour back."""
    result = enable(serve_args)
    OPT_OUT_MARKER.unlink(missing_ok=True)
    return result


def disable_and_remember_opt_out() -> str:
    """Disable autostart and remember that a person did it on purpose, so
    `ensure_enabled_by_default` leaves it off on the next `agnview serve`."""
    result = disable()
    OPT_OUT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    OPT_OUT_MARKER.write_text("")
    return result


def ensure_enabled_by_default(serve_args: Sequence[str] = ()) -> Optional[str]:
    """Keep the login registration in step with how the hub is being served.

    Registers autostart on every `agnview serve` unless a person explicitly
    turned it off before, and rewrites the registration when the options
    change. Without the rewrite, the first `agnview serve` fixed a flag-less
    command forever: somebody who later served with --listen-lan, or on
    another port, still came back after a reboot bound to loopback on 8765,
    and the phone that had been paired to the LAN address lost the hub with
    no error anywhere.

    Failures are swallowed, since a hub that cannot write a registry value,
    plist or desktop entry must still serve. Returns "enabled" when it just
    turned autostart on, "updated" when it rewrote an existing registration,
    and None when it left everything alone, so the caller can print a one-off
    notice rather than one on every start.
    """
    if OPT_OUT_MARKER.exists() or DESKTOP_SETTINGS.exists():
        # The desktop app owns starting at sign-in on a machine where it is
        # installed. A registration from `agnview serve` started a second,
        # browser-only hub at sign-in that took the port first.
        return None
    try:
        if not status():
            enable(serve_args)
            return "enabled"
        wanted = _resolve_command_string(serve_args)
        current = registered_command()
        if current is not None and current.strip() != wanted:
            enable(serve_args)
            return "updated"
    except Exception:
        pass
    return None
