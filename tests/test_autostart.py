"""Unit tests for the Windows autostart path in agent_relay.core.autostart.

Runs on any platform (including the Linux CI runner): it monkeypatches
sys.platform to "win32" and injects a fake winreg module into sys.modules,
so the Windows code path is exercised without a real Windows registry.
"""

import sys
import types

import pytest

from agent_relay.core import autostart


class FakeWinReg:
    """Minimal in-memory stand-in for the winreg module, enough to
    exercise autostart's Windows enable/disable/status logic."""

    HKEY_CURRENT_USER = "HKEY_CURRENT_USER"
    KEY_SET_VALUE = 1
    KEY_READ = 2
    REG_SZ = 1

    def __init__(self):
        # The real Run key always exists on Windows; only the AgnView
        # value under it may or may not be present. Start with the key
        # present and empty, matching that.
        self.values = {autostart.RUN_KEY_PATH: {}}

    def CreateKeyEx(self, hive, path, reserved, access):
        self.values.setdefault(path, {})
        return ("key", path)

    def OpenKey(self, hive, path, reserved, access):
        if path not in self.values:
            raise FileNotFoundError(path)
        return ("key", path)

    def SetValueEx(self, key, name, reserved, val_type, value):
        _, path = key
        self.values.setdefault(path, {})[name] = value

    def QueryValueEx(self, key, name):
        _, path = key
        bucket = self.values.get(path, {})
        if name not in bucket:
            raise FileNotFoundError(name)
        return (bucket[name], self.REG_SZ)

    def DeleteValue(self, key, name):
        _, path = key
        bucket = self.values.get(path, {})
        if name not in bucket:
            raise FileNotFoundError(name)
        del bucket[name]

    def CloseKey(self, key):
        pass


@pytest.fixture
def fake_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    fake = FakeWinReg()
    fake_module = types.SimpleNamespace(**{k: getattr(fake, k) for k in dir(fake) if not k.startswith("_")})
    monkeypatch.setitem(sys.modules, "winreg", fake_module)
    return fake


def test_windows_status_false_when_never_enabled(fake_windows):
    assert autostart.status() is False


def test_windows_enable_sets_registry_value(fake_windows):
    result = autostart.enable()
    assert "Autostart enabled" in result
    assert autostart.status() is True


def test_windows_enable_command_prefers_agnview_on_path(fake_windows, monkeypatch):
    monkeypatch.setattr(autostart.shutil, "which", lambda name: r"C:\Users\test\AppData\Roaming\Python\Scripts\agnview.exe")
    result = autostart.enable()
    assert "agnview.exe" in result
    assert "serve" in result


def test_windows_enable_falls_back_to_module_invocation(fake_windows, monkeypatch):
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
    result = autostart.enable()
    assert "-m" in result
    assert "agent_relay.cli.main" in result


def test_windows_disable_is_idempotent(fake_windows):
    # Disabling when nothing was ever enabled should not raise.
    result = autostart.disable()
    assert "already disabled" in result
    assert autostart.status() is False


def test_windows_enable_then_disable_round_trip(fake_windows):
    autostart.enable()
    assert autostart.status() is True

    result = autostart.disable()
    assert "Autostart disabled" in result
    assert autostart.status() is False
