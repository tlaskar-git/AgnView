"""The Windows desktop app keeps its settings in ~/.agnview/desktop.json and
registers a command that starts it hidden in the tray at sign-in."""

import json
import sys

from agent_relay.desktop import app as desktop


def test_settings_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    assert desktop.load_settings() == {"port": desktop.DEFAULT_PORT, "autostart": False}


def test_settings_keep_known_keys_only(tmp_path, monkeypatch):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"port": 9100, "autostart": False, "other": 1}))
    monkeypatch.setattr(desktop, "SETTINGS_PATH", path)
    assert desktop.load_settings() == {"port": 9100, "autostart": False}


def test_settings_survive_a_corrupt_file(tmp_path, monkeypatch):
    path = tmp_path / "desktop.json"
    path.write_text("{not json")
    monkeypatch.setattr(desktop, "SETTINGS_PATH", path)
    assert desktop.load_settings()["port"] == desktop.DEFAULT_PORT


def test_settings_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "sub" / "desktop.json")
    desktop.save_settings({"port": 9200, "autostart": True})
    assert desktop.load_settings() == {"port": 9200, "autostart": True}


def test_frozen_build_registers_itself_minimized(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Program Files\AgnView\AgnView.exe")
    assert desktop.launch_command() == (r"C:\Program Files\AgnView\AgnView.exe", "--minimized")


def test_source_install_registers_the_module_minimized(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    _program, arguments = desktop.launch_command()
    assert arguments == "-m agent_relay.desktop --minimized"


def test_sign_in_task_starts_hidden_after_a_delay_and_retries():
    xml = desktop.task_xml(r"C:\Apps\AgnView & Co\AgnView.exe", "--minimized", r"HOST\user")
    assert "<LogonTrigger>" in xml
    assert r"<UserId>HOST\user</UserId>" in xml
    assert f"<Delay>{desktop.SIGN_IN_DELAY}</Delay>" in xml
    assert "<RestartOnFailure>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml
    assert r"<Command>C:\Apps\AgnView &amp; Co\AgnView.exe</Command>" in xml
    assert "<Arguments>--minimized</Arguments>" in xml


def test_start_with_windows_is_off_until_turned_on(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    assert desktop.autostart_enabled() is False


def test_changing_start_with_windows_saves_the_choice(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    desktop.save_settings({"port": 9300, "autostart": False})
    calls = []
    monkeypatch.setattr(desktop, "set_autostart", lambda enabled: calls.append(enabled))

    desktop.change_autostart(True)

    assert calls == [True]
    assert desktop.load_settings() == {"port": 9300, "autostart": True}
    assert desktop.autostart_enabled() is True


def test_a_refused_change_saves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")

    def refuse(enabled):
        raise OSError("Access is denied.")

    monkeypatch.setattr(desktop, "set_autostart", refuse)
    try:
        desktop.change_autostart(True)
    except OSError:
        pass
    assert desktop.autostart_enabled() is False


def test_the_close_button_closes_the_app():
    app = desktop.DesktopApp(hub=None, settings={}, start_hidden=False)
    app.window = object()
    assert app.on_closing() is True
    assert app.quitting is True


def test_the_dashboard_switch_drives_the_desktop_setting(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from agent_relay.api.app import create_app

    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    monkeypatch.setattr(desktop, "set_autostart", lambda enabled: None)
    monkeypatch.setenv(desktop.DESKTOP_ENV, "1")
    client = TestClient(create_app(db_path=str(tmp_path / "hub.db")))

    assert client.get("/api/system/autostart").json()["enabled"] is False
    assert client.post("/api/system/autostart", json={"enable": True}).json()["enabled"] is True
    assert desktop.autostart_enabled() is True
    assert client.post("/api/system/autostart", json={"enable": False}).json()["enabled"] is False


def test_the_desktop_app_has_its_own_port():
    # 8765 is what `agnview serve` uses by default.
    assert desktop.DEFAULT_PORT != 8765


def test_a_busy_port_moves_to_the_next_free_one(monkeypatch):
    busy = {18845, 18846}
    monkeypatch.setattr(desktop, "port_in_use", lambda port: port in busy)
    assert desktop.choose_port(18845) == 18847


def test_no_free_port_in_range_gives_none(monkeypatch):
    monkeypatch.setattr(desktop, "port_in_use", lambda port: True)
    assert desktop.choose_port(18845) is None


def test_agnview_serve_leaves_sign_in_to_the_desktop_app(tmp_path, monkeypatch):
    from agent_relay.core import autostart

    marker = tmp_path / "desktop.json"
    marker.write_text("{}")
    monkeypatch.setattr(autostart, "DESKTOP_SETTINGS", marker)
    monkeypatch.setattr(autostart, "OPT_OUT_MARKER", tmp_path / "absent")
    monkeypatch.setattr(autostart, "status", lambda: (_ for _ in ()).throw(AssertionError("checked")))
    monkeypatch.setattr(autostart, "enable", lambda args=(): (_ for _ in ()).throw(AssertionError("registered")))
    assert autostart.ensure_enabled_by_default(["--port", "8845"]) is None
