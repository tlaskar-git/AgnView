"""The iroh transport must cost the user nothing to run.

No account, no key to manage, no prompt and no required setting. These tests
hold that claim to the code rather than to the ADR.
"""

import inspect
from pathlib import Path

from agent_relay.core import config as config_module
from agent_relay.core import iroh_transport
from agent_relay.core.config import load_config


def test_a_hub_with_no_configuration_file_still_enables_iroh(tmp_path, monkeypatch):
    monkeypatch.setenv("AGNVIEW_CONFIG", str(tmp_path / "absent" / "config.yaml"))
    config = load_config()
    assert config.iroh_enabled is True
    assert config.relay_url == ""
    assert config.is_valid


def test_every_setting_has_a_working_default(tmp_path, monkeypatch):
    """The written template must parse to the same defaults as an absent file."""
    written = tmp_path / "config.yaml"
    monkeypatch.setenv("AGNVIEW_CONFIG", str(written))
    from_template = load_config()

    monkeypatch.setenv("AGNVIEW_CONFIG", str(tmp_path / "missing.yaml"))
    Path(tmp_path / "missing.yaml").unlink(missing_ok=True)
    from_nothing = load_config()

    assert from_template.relay_url == from_nothing.relay_url == ""
    assert from_template.iroh_enabled == from_nothing.iroh_enabled is True


def test_the_transport_never_prompts():
    source = inspect.getsource(iroh_transport)
    assert "input(" not in source
    assert "getpass" not in source


def test_the_endpoint_key_is_created_without_asking(tmp_path):
    key_file = tmp_path / "iroh_secret"
    assert not key_file.exists()
    secret = iroh_transport.get_or_create_iroh_secret(key_file)
    assert len(secret) == 32
    assert key_file.exists()


def test_the_bundled_relays_need_no_credential():
    """An empty relay_url must carry no token, user or key alongside it."""
    template = config_module.DEFAULT_CONFIG_TEMPLATE
    for credential in ("token", "api_key", "username", "password", "secret"):
        assert credential not in template

    transport = iroh_transport.IrohTransport()
    status = transport.status()
    assert status["configured_relay_url"] is None
