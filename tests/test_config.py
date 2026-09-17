"""Tests for ~/.agnview/config.yaml and the relay_url setting."""

import logging

import pytest
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app
from agent_relay.core.config import (
    ConfigError,
    ensure_default_config,
    load_config,
    validate_relay_url,
)


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("AGNVIEW_CONFIG", str(path))
    return path


def test_the_default_config_is_written_on_first_run(config_path):
    assert not config_path.exists()
    config = load_config()
    assert config_path.exists()
    assert "relay_url" in config_path.read_text(encoding="utf-8")
    assert config.relay_url == ""
    assert config.iroh_enabled is True
    assert config.is_valid


def test_an_empty_relay_url_means_the_bundled_relays(config_path):
    config_path.write_text('relay_url: ""\n', encoding="utf-8")
    config = load_config()
    assert config.relay_url == ""
    assert config.is_valid


def test_a_relay_url_overrides_the_bundled_relays(config_path):
    config_path.write_text("relay_url: https://relay.example.com\n", encoding="utf-8")
    config = load_config()
    assert config.relay_url == "https://relay.example.com"
    assert config.is_valid


@pytest.mark.parametrize("value", [
    "relay.example.com",
    "ftp://relay.example.com",
    "https://",
    "https://relay.example.com?token=abc",
    "https://relay.example.com#fragment",
    17,
])
def test_an_invalid_relay_url_is_refused(value):
    with pytest.raises(ConfigError):
        validate_relay_url(value)


def test_an_invalid_relay_url_is_reported_loudly(config_path, caplog):
    config_path.write_text("relay_url: not-a-url\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="agnview.config"):
        config = load_config()

    assert not config.is_valid
    assert any("relay_url" in message for message in config.errors)
    assert "the iroh transport will not start" in caplog.text


def test_unreadable_yaml_is_reported_rather_than_ignored(config_path, caplog):
    config_path.write_text("relay_url: [unclosed\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger="agnview.config"):
        config = load_config()
    assert not config.is_valid
    assert "not readable YAML" in caplog.text


def test_a_non_boolean_iroh_enabled_is_refused(config_path):
    config_path.write_text("iroh_enabled: perhaps\n", encoding="utf-8")
    config = load_config()
    assert not config.is_valid
    assert any("iroh_enabled" in message for message in config.errors)


def test_ensure_default_config_does_not_overwrite(config_path):
    config_path.write_text("relay_url: https://relay.example.com\n", encoding="utf-8")
    ensure_default_config()
    assert config_path.read_text(encoding="utf-8") == "relay_url: https://relay.example.com\n"


def test_the_hub_starts_with_an_invalid_relay_url_and_refuses_iroh(config_path, tmp_path, monkeypatch):
    monkeypatch.setenv("AGNVIEW_IROH", "1")
    config_path.write_text("relay_url: not-a-url\n", encoding="utf-8")

    app = create_app(db_path=str(tmp_path / "badrelay.db"))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        data = client.get("/api/transport").json()

    assert app.state.iroh.state == "disabled"
    assert "relay_url" in (app.state.iroh.status()["error"] or "")
    assert data["config"]["errors"]
    assert data["iroh"]["ticket"] is None


def test_a_valid_relay_url_reaches_the_transport(config_path, tmp_path, monkeypatch):
    monkeypatch.setenv("AGNVIEW_IROH", "0")
    config_path.write_text("relay_url: https://relay.example.com\n", encoding="utf-8")

    app = create_app(db_path=str(tmp_path / "goodrelay.db"))
    assert app.state.iroh.status()["configured_relay_url"] == "https://relay.example.com"


def test_iroh_enabled_false_keeps_the_hub_lan_only(config_path, tmp_path, monkeypatch):
    monkeypatch.setenv("AGNVIEW_IROH", "1")
    config_path.write_text("iroh_enabled: false\n", encoding="utf-8")

    app = create_app(db_path=str(tmp_path / "lanonly.db"))
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
    assert app.state.iroh.state == "disabled"
    assert "iroh_enabled is false" in (app.state.iroh.status()["error"] or "")
