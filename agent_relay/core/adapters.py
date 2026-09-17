"""Agent Adapters configuration and loader for AgnView.

Loads CLI agent definitions from YAML file with validation and placeholder substitutions.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel
import yaml

DEFAULT_AGENTS_DIR = Path.home() / ".agnview"
DEFAULT_AGENTS_FILE = DEFAULT_AGENTS_DIR / "agents.yaml"

ALLOWED_PLACEHOLDERS = {"{prompt}", "{workspace}", "{session_id}", "{model}", "{effort}", "{skill}"}
ALLOWED_ADAPTER_KEYS = {"display", "command", "cwd", "icon", "colour", "enabled"}

DEFAULT_AGENTS_YAML = """# AgnView Agent Adapters Specification
# Each adapter defines a CLI agent execution command and metadata.
# Available placeholders: {prompt}, {workspace}, {session_id}, {model}, {effort}, {skill}

agents:
  claude_code:
    display: Claude Code
    command: ["claude", "-p", "{prompt}"]
    cwd: "{workspace}"
    icon: sparkles
    colour: "#FBBF24"
    enabled: true

  codex:
    display: Codex / ChatGPT
    command: ["codex", "exec", "--skip-git-repo-check", "{prompt}"]
    cwd: "{workspace}"
    icon: terminal
    colour: "#10A37F"
    enabled: true

  antigravity:
    display: AntiGravity
    command: ["agy", "--print", "{prompt}", "--dangerously-skip-permissions"]
    cwd: "{workspace}"
    icon: compass
    colour: "#8B5CF6"
    enabled: true

  gemini:
    display: Google Gemini
    command: ["gemini", "-p", "{prompt}", "--yolo", "--skip-trust"]
    cwd: "{workspace}"
    icon: cpu
    colour: "#3B82F6"
    enabled: true
"""


class AgentAdapter(BaseModel):
    id: str
    display: str
    command: List[str]
    cwd: Optional[str] = "{workspace}"
    icon: Optional[str] = "terminal"
    colour: Optional[str] = "#38BDF8"
    enabled: bool = True


class AgentAdapterPublic(BaseModel):
    id: str
    display: str
    cwd: Optional[str] = "{workspace}"
    icon: Optional[str] = "terminal"
    colour: Optional[str] = "#38BDF8"
    enabled: bool = True


class AdapterValidationError(Exception):
    pass


class AdapterManager:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or DEFAULT_AGENTS_FILE
        self.adapters: Dict[str, AgentAdapter] = {}
        self.load()

    def ensure_default_file(self) -> bool:
        """Create ~/.agnview/agents.yaml if it does not exist."""
        if not self.config_path.exists():
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self.config_path.write_text(DEFAULT_AGENTS_YAML, encoding="utf-8")
            return True
        return False

    def load(self) -> Dict[str, AgentAdapter]:
        """Load and validate adapters from configuration file."""
        self.ensure_default_file()

        try:
            content = self.config_path.read_text(encoding="utf-8")
        except Exception as e:
            raise AdapterValidationError(f"Could not read agents config file {self.config_path}: {e}")

        # Line-numbered validation for keys
        lines = content.splitlines()
        try:
            raw_data = yaml.safe_load(content)
        except yaml.YAMLError as e:
            line_no = getattr(e, 'problem_mark', None)
            line_str = f" at line {line_no.line + 1}" if line_no else ""
            raise AdapterValidationError(f"Invalid YAML in agents config file {self.config_path}{line_str}: {e}")

        if not raw_data or "agents" not in raw_data:
            raise AdapterValidationError(f"Missing 'agents' root mapping in {self.config_path}")

        agents_dict = raw_data.get("agents")
        if not isinstance(agents_dict, dict):
            raise AdapterValidationError(f"'agents' in {self.config_path} must be a dictionary")

        loaded: Dict[str, AgentAdapter] = {}

        for agent_id, agent_cfg in agents_dict.items():
            if not isinstance(agent_cfg, dict):
                raise AdapterValidationError(f"Agent '{agent_id}' configuration must be a mapping in {self.config_path}")

            # Check for unknown keys and report specific line
            for key in agent_cfg.keys():
                if key not in ALLOWED_ADAPTER_KEYS:
                    # Find line number of key in content
                    found_line = 1
                    for idx, line in enumerate(lines, start=1):
                        if f"{key}:" in line or f"\"{key}\":" in line:
                            found_line = idx
                            break
                    raise AdapterValidationError(
                        f"Unknown key '{key}' for agent '{agent_id}' at line {found_line} in {self.config_path}."
                    )

            if "display" not in agent_cfg or not str(agent_cfg["display"]).strip():
                # Find line
                found_line = 1
                for idx, line in enumerate(lines, start=1):
                    if f"{agent_id}:" in line:
                        found_line = idx
                        break
                raise AdapterValidationError(
                    f"Missing or empty 'display' name for agent '{agent_id}' at line {found_line} in {self.config_path}."
                )

            if "command" not in agent_cfg:
                found_line = 1
                for idx, line in enumerate(lines, start=1):
                    if f"{agent_id}:" in line:
                        found_line = idx
                        break
                raise AdapterValidationError(
                    f"Missing 'command' list for agent '{agent_id}' at line {found_line} in {self.config_path}."
                )

            command = agent_cfg.get("command")
            if not isinstance(command, list) or len(command) == 0:
                found_line = 1
                for idx, line in enumerate(lines, start=1):
                    if "command:" in line:
                        found_line = idx
                        break
                raise AdapterValidationError(
                    f"Malformed 'command' array for agent '{agent_id}' at line {found_line} in {self.config_path}. Must be a non-empty list of strings."
                )

            loaded[agent_id] = AgentAdapter(
                id=agent_id,
                display=agent_cfg["display"],
                command=[str(c) for c in command],
                cwd=agent_cfg.get("cwd", "{workspace}"),
                icon=agent_cfg.get("icon", "terminal"),
                colour=agent_cfg.get("colour", "#38BDF8"),
                enabled=bool(agent_cfg.get("enabled", True))
            )

        self.adapters = loaded
        return self.adapters

    def reload(self) -> Dict[str, AgentAdapter]:
        """Re-read configuration without restart."""
        return self.load()

    def get_public_adapters(self) -> List[AgentAdapterPublic]:
        """Return adapters without command array (browser safe)."""
        return [
            AgentAdapterPublic(
                id=a.id,
                display=a.display,
                cwd=a.cwd,
                icon=a.icon,
                colour=a.colour,
                enabled=a.enabled
            )
            for a in self.adapters.values()
            if a.enabled
        ]

    def get_adapter(self, agent_id: str) -> Optional[AgentAdapter]:
        return self.adapters.get(agent_id)

    def get_adapters(self) -> Dict[str, AgentAdapter]:
        return self.adapters

    def build_command(
        self,
        agent_id: str,
        prompt: str = "",
        workspace: str = "",
        session_id: str = "",
        model: Optional[str] = None,
        effort: Optional[str] = None,
        skill: Optional[str] = None
    ) -> tuple[List[str], str]:
        """Build command and resolved working directory for an agent adapter."""
        adapter = self.get_adapter(agent_id)
        if not adapter:
            raise AdapterValidationError(f"Unknown agent adapter '{agent_id}'")

        substitutions = {
            "{prompt}": prompt,
            "{workspace}": workspace or os.getcwd(),
            "{session_id}": session_id or "",
            "{model}": model or "",
            "{effort}": effort or "",
            "{skill}": skill or ""
        }

        cmd: List[str] = []
        for part in adapter.command:
            resolved = part
            for placeholder, val in substitutions.items():
                if placeholder in resolved:
                    resolved = resolved.replace(placeholder, val)
            cmd.append(resolved)

        cwd_resolved = adapter.cwd or "{workspace}"
        for placeholder, val in substitutions.items():
            if placeholder in cwd_resolved:
                cwd_resolved = cwd_resolved.replace(placeholder, val)

        return cmd, cwd_resolved

    def is_valid_role(self, role: str) -> bool:
        cleaned = role.strip().lower()
        if cleaned in ("user", "system"):
            return True
        role_part = cleaned.split("@")[0] if "@" in cleaned else cleaned
        return role_part in self.adapters
