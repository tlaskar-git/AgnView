"""Unit tests for agent adapters configuration, validation, and execution."""

import pytest
from agent_relay.core.adapters import AdapterManager, AdapterValidationError


def test_default_adapters_creation(tmp_path):
    config_file = tmp_path / "agents.yaml"
    manager = AdapterManager(config_path=config_file)
    assert config_file.exists()
    adapters = manager.get_adapters()
    assert "claude_code" in adapters
    assert "codex" in adapters
    assert "antigravity" in adapters
    assert "gemini" in adapters
    assert manager.is_valid_role("claude_code")
    assert manager.is_valid_role("gemini")
    assert not manager.is_valid_role("nonexistent_agent")


def test_public_adapters_do_not_expose_command(tmp_path):
    config_file = tmp_path / "agents.yaml"
    manager = AdapterManager(config_path=config_file)
    public = manager.get_public_adapters()
    for pub in public:
        assert hasattr(pub, "id")
        assert hasattr(pub, "display")
        assert not hasattr(pub, "command")


def test_build_command_substitutions(tmp_path):
    config_file = tmp_path / "agents.yaml"
    yaml_content = """agents:
  custom_cli:
    display: Custom CLI
    command: ["tool", "--prompt", "{prompt}", "--dir", "{workspace}", "--sess", "{session_id}"]
    cwd: "{workspace}"
"""
    config_file.write_text(yaml_content, encoding="utf-8")
    manager = AdapterManager(config_path=config_file)
    cmd, cwd = manager.build_command(
        "custom_cli",
        prompt="hello world",
        workspace="/test/dir",
        session_id="sess-123"
    )
    assert cmd == ["tool", "--prompt", "hello world", "--dir", "/test/dir", "--sess", "sess-123"]
    assert cwd == "/test/dir"


def test_validation_malformed_yaml(tmp_path):
    config_file = tmp_path / "agents.yaml"
    config_file.write_text("agents:\n  claude:\n    display: [unclosed", encoding="utf-8")
    with pytest.raises(AdapterValidationError) as exc:
        AdapterManager(config_path=config_file)
    assert "line" in str(exc.value).lower()


def test_validation_missing_agents_root(tmp_path):
    config_file = tmp_path / "agents.yaml"
    config_file.write_text("something_else:\n  foo: bar\n", encoding="utf-8")
    with pytest.raises(AdapterValidationError) as exc:
        AdapterManager(config_path=config_file)
    assert "Missing 'agents' root mapping" in str(exc.value)


def test_validation_unknown_key_with_line_number(tmp_path):
    config_file = tmp_path / "agents.yaml"
    yaml_content = """agents:
  my_agent:
    display: My Agent
    command: ["echo", "hi"]
    invalid_field: true
"""
    config_file.write_text(yaml_content, encoding="utf-8")
    with pytest.raises(AdapterValidationError) as exc:
        AdapterManager(config_path=config_file)
    msg = str(exc.value)
    assert "Unknown key 'invalid_field'" in msg
    assert "line 5" in msg


def test_validation_missing_command_list(tmp_path):
    config_file = tmp_path / "agents.yaml"
    yaml_content = """agents:
  my_agent:
    display: My Agent
"""
    config_file.write_text(yaml_content, encoding="utf-8")
    with pytest.raises(AdapterValidationError) as exc:
        AdapterManager(config_path=config_file)
    assert "Missing 'command' list" in str(exc.value)


def test_validation_empty_command_list(tmp_path):
    config_file = tmp_path / "agents.yaml"
    yaml_content = """agents:
  my_agent:
    display: My Agent
    command: []
"""
    config_file.write_text(yaml_content, encoding="utf-8")
    with pytest.raises(AdapterValidationError) as exc:
        AdapterManager(config_path=config_file)
    assert "Malformed 'command' array" in str(exc.value)

