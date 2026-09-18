"""Unit tests for the Windows autostart path in agent_relay.core.autostart.

Runs on any platform (including the Linux CI runner): it monkeypatches
sys.platform to "win32" and injects a fake winreg module into sys.modules,
so the Windows code path is exercised without a real Windows registry.
"""

import sys
import types
from pathlib import Path

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
    assert autostart.ensure_enabled_by_default() == "enabled"
    assert autostart.status() is True


def test_ensure_enabled_by_default_is_a_noop_once_already_on(fake_windows, fake_opt_out_marker):
    autostart.enable()
    assert autostart.ensure_enabled_by_default() is None
    assert autostart.status() is True


def test_the_registered_command_carries_the_serve_options(fake_windows, fake_opt_out_marker):
    """A hub served with --listen-lan must come back with --listen-lan."""
    assert autostart.ensure_enabled_by_default(["--listen-lan"]) == "enabled"
    assert "--listen-lan" in autostart.registered_command()


def test_changing_the_serve_options_rewrites_the_registration(fake_windows, fake_opt_out_marker):
    """The flag-less command registered on the first serve used to stick.

    A person who paired a phone, then started serving with --listen-lan, came
    back after a reboot bound to loopback with no error anywhere.
    """
    autostart.ensure_enabled_by_default()
    assert "--listen-lan" not in autostart.registered_command()

    assert autostart.ensure_enabled_by_default(["--listen-lan", "--port", "9100"]) == "updated"
    registered = autostart.registered_command()
    assert "--listen-lan" in registered
    assert "--port 9100" in registered

    # And once it matches, it is left alone.
    assert autostart.ensure_enabled_by_default(["--listen-lan", "--port", "9100"]) is None


def test_the_registration_never_carries_the_pairing_token(fake_windows, fake_opt_out_marker):
    autostart.ensure_enabled_by_default(["--listen-lan"])
    assert "--token" not in autostart.registered_command()


def test_ensure_enabled_by_default_respects_explicit_opt_out(fake_windows, fake_opt_out_marker):
    autostart.disable_and_remember_opt_out()
    assert fake_opt_out_marker.exists()

    assert autostart.ensure_enabled_by_default() is None
    assert autostart.status() is False


def test_enable_and_clear_opt_out_removes_the_marker(fake_windows, fake_opt_out_marker):
    autostart.disable_and_remember_opt_out()
    assert fake_opt_out_marker.exists()

    autostart.enable_and_clear_opt_out()
    assert not fake_opt_out_marker.exists()
    assert autostart.status() is True

    # A later `agnview serve` should now leave it alone, not fight the
    # person's decision to turn it back on by hand.
    assert autostart.ensure_enabled_by_default() is None


# --- The dashboard and the API must report the registration the CLI writes ---

def test_the_api_reports_the_same_registration_the_cli_writes(tmp_path, monkeypatch):
    """The API used to look for a .cmd file only it ever wrote.

    On a normal install, where `agnview serve` has registered autostart, it
    answered "off" while autostart was on, and switching it off deleted a file
    that was not the registration.
    """
    from fastapi.testclient import TestClient

    from agent_relay.api import routes
    from agent_relay.api.app import create_app

    state = {"enabled": True}
    monkeypatch.setattr(routes.autostart, "status", lambda: state["enabled"])
    monkeypatch.setattr(routes.autostart, "registered_command", lambda: "agnview serve --listen-lan")

    def fake_disable():
        state["enabled"] = False
        return "Autostart disabled."

    def fake_enable(serve_args=()):
        state["enabled"] = True
        return "Autostart enabled."

    monkeypatch.setattr(routes.autostart, "disable_and_remember_opt_out", fake_disable)
    monkeypatch.setattr(routes.autostart, "enable_and_clear_opt_out", fake_enable)

    client = TestClient(create_app(db_path=str(tmp_path / "autostart.db")))

    assert client.get("/api/system/autostart").json()["enabled"] is True
    assert client.get("/api/system/capabilities").json()["autostart_enabled"] is True

    # Switching it off works on every platform, not only Windows.
    off = client.post("/api/system/autostart", json={"enable": False}).json()
    assert off["success"] is True
    assert off["enabled"] is False
    assert client.get("/api/system/autostart").json()["enabled"] is False

    on = client.post("/api/system/autostart", json={"enable": True}).json()
    assert on["enabled"] is True


def test_the_dashboard_reads_the_field_the_api_returns():
    """The toggle read data.autostart, which the API never sent, so it always
    rendered off however the setting really stood."""
    dashboard = (
        Path(autostart.__file__).parent.parent / "web" / "templates" / "index.html"
    ).read_text(encoding="utf-8", errors="replace")

    assert "toggle.checked = !!data.enabled" in dashboard
    assert "data.autostart" not in dashboard
