"""The Windows desktop app keeps its settings in ~/.agnview/desktop.json and
registers a command that starts it hidden in the tray at sign-in."""

import json
import sys

from agent_relay.desktop import app as desktop


def test_settings_default_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "SETTINGS_PATH", tmp_path / "desktop.json")
    assert desktop.load_settings() == {"port": desktop.DEFAULT_PORT, "autostart": True}


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
