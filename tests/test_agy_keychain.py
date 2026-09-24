"""AntiGravity's sign-in on macOS, read from the login Keychain. Runs on every
platform with the security command faked."""

import base64
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_relay.core.usage.adapters import agy_cloud

SIGN_IN = {"token": {"access_token": "test-access", "refresh_token": "test-refresh"}, "id_token": "a.b.c"}
RAW = json.dumps(SIGN_IN)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(agy_cloud, "_keychain_refused", False)


def test_base64_from_the_go_keyring_is_decoded():
    stored = "go-keyring-base64:" + base64.b64encode(RAW.encode()).decode()
    assert agy_cloud.decode_keychain_secret(stored + "\n") == RAW.encode()


def test_hex_from_the_go_keyring_is_decoded():
    stored = "go-keyring-encoded:" + RAW.encode().hex()
    assert agy_cloud.decode_keychain_secret(stored) == RAW.encode()


def test_plain_json_is_kept():
    assert agy_cloud.decode_keychain_secret(RAW + "\n") == RAW.encode()


def test_hex_printed_by_security_is_decoded():
    stored = ("go-keyring-base64:" + base64.b64encode(RAW.encode()).decode()).encode().hex()
    assert agy_cloud.decode_keychain_secret(stored) == RAW.encode()


def test_empty_or_broken_values_give_none():
    assert agy_cloud.decode_keychain_secret("") is None
    assert agy_cloud.decode_keychain_secret("go-keyring-base64:@@@") is None
    assert agy_cloud.decode_keychain_secret("go-keyring-encoded:zz") is None


def _fake_security(monkeypatch, answers):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        code, out, err = answers.pop(0) if answers else (44, "", "not found")
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_macos_reads_the_gemini_antigravity_item(monkeypatch):
    stored = "go-keyring-base64:" + base64.b64encode(RAW.encode()).decode()
    calls = _fake_security(monkeypatch, [(0, stored + "\n", "")])

    assert agy_cloud.read_sign_in() == SIGN_IN
    assert calls[0] == ["security", "find-generic-password", "-s", "gemini", "-a", "antigravity", "-w"]


def test_macos_without_the_item_gives_none_without_error(monkeypatch):
    calls = _fake_security(monkeypatch, [])
    assert agy_cloud.read_sign_in() is None
    assert len(calls) == 2


def test_a_refused_keychain_prompt_is_not_asked_again(monkeypatch):
    calls = _fake_security(monkeypatch, [(128, "", "User canceled the operation.")])
    assert agy_cloud.read_sign_in() is None
    assert agy_cloud.read_sign_in() is None
    assert len(calls) == 1


def test_a_missing_security_command_is_harmless(monkeypatch):
    def run(command, **kwargs):
        raise FileNotFoundError("security")

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", run)
    assert agy_cloud.read_sign_in() is None


def test_other_platforms_never_run_security(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert agy_cloud.read_sign_in() is None


def test_the_language_server_is_found_inside_the_app_bundle(tmp_path):
    bundle = tmp_path / "Antigravity.app"
    binary = bundle / "Contents" / "Resources" / "app" / "extensions" / "antigravity" / "bin" / "language_server_macos_arm"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"\0")
    (bundle / "Contents" / "Resources" / "other.txt").write_text("x")

    assert agy_cloud.macos_language_servers([str(bundle), str(tmp_path / "Absent.app")]) == [str(binary)]


def test_client_secrets_are_read_from_the_mac_binary(tmp_path, monkeypatch):
    binary = tmp_path / "Antigravity.app" / "Contents" / "Resources" / "bin" / "language_server"
    binary.parent.mkdir(parents=True)
    fake = b"GOCSPX-" + b"A" * 28
    binary.write_bytes(b"prefix" + fake + b"suffix" + fake)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(agy_cloud, "MACOS_APP_BUNDLES", (str(tmp_path / "Antigravity.app"),))
    assert agy_cloud.installed_client_secrets() == [fake.decode()]
