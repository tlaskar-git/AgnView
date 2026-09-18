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
    # shutil.which's real implementation branches on sys.platform too, and
    # calls Windows-only helpers that do not exist when the tests run on
    # Linux/macOS CI with sys.platform faked to "win32". Give it a
    # deterministic default here; tests that care override it themselves.
    monkeypatch.setattr(autostart.shutil, "which", lambda name: None)
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


@pytest.fixture
def fake_opt_out_marker(monkeypatch, tmp_path):
    """Point the opt-out marker at a throwaway path so these tests never
    touch a real person's ~/.agnview directory."""
    marker = tmp_path / ".agnview" / ".autostart_opt_out"
    monkeypatch.setattr(autostart, "OPT_OUT_MARKER", marker)
    return marker


def test_ensure_enabled_by_default_turns_on_a_fresh_install(fake_windows, fake_opt_out_marker):
    assert autostart.status() is False
    changed = autostart.ensure_enabled_by_default()
    assert changed is True
    assert autostart.status() is True


def test_ensure_enabled_by_default_is_a_noop_once_already_on(fake_windows, fake_opt_out_marker):
    autostart.enable()
    changed = autostart.ensure_enabled_by_default()
    assert changed is False
    assert autostart.status() is True


def test_ensure_enabled_by_default_respects_explicit_opt_out(fake_windows, fake_opt_out_marker):
    autostart.disable_and_remember_opt_out()
    assert fake_opt_out_marker.exists()

    changed = autostart.ensure_enabled_by_default()
    assert changed is False
    assert autostart.status() is False


def test_enable_and_clear_opt_out_removes_the_marker(fake_windows, fake_opt_out_marker):
    autostart.disable_and_remember_opt_out()
    assert fake_opt_out_marker.exists()

    autostart.enable_and_clear_opt_out()
    assert not fake_opt_out_marker.exists()
    assert autostart.status() is True

    # A later `agnview serve` should now leave it alone, not fight the
    # person's decision to turn it back on by hand.
    changed = autostart.ensure_enabled_by_default()
    assert changed is False
