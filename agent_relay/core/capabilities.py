"""What each agent can be asked to run: models and effort levels.

One list, read by GET /api/system/capabilities for the dashboard and the phone,
and by job validation, so a task can only ask for what a dispatch could.
"""

from typing import Dict, List, Optional

MODELS: Dict[str, List[Dict[str, str]]] = {
    "claude_code": [
        {"id": "claude-fable-5-1", "name": "Fable 5.1"},
        {"id": "claude-fable-5", "name": "Fable 5"},
        {"id": "claude-opus-5", "name": "Opus 5"},
        {"id": "claude-opus-4-8", "name": "Opus 4.8"},
        {"id": "claude-opus-4-7", "name": "Opus 4.7"},
        {"id": "claude-opus-4-6-thinking", "name": "Opus 4.6"},
        {"id": "claude-sonnet-5", "name": "Sonnet 5"},
        {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
        {"id": "claude-3-7-sonnet-20250219", "name": "Sonnet 3.7"},
        {"id": "claude-3-5-sonnet-20241022", "name": "Sonnet 3.5"},
        {"id": "claude-3-5-haiku-20241022", "name": "Haiku 3.5"},
        {"id": "claude-3-opus-20240229", "name": "Claude 3 Opus"}
    ],
    "codex": [
        {"id": "gpt-6-astra", "name": "GPT-6 Astra"},
        {"id": "gpt-5-codex", "name": "GPT-5 Codex"},
        {"id": "gpt-5", "name": "GPT-5"},
        {"id": "o3-mini", "name": "o3-mini"},
        {"id": "o3", "name": "o3"},
        {"id": "o1", "name": "o1"},
        {"id": "o1-mini", "name": "o1-mini"},
        {"id": "gpt-4.5-preview", "name": "GPT-4.5 Preview"},
        {"id": "gpt-4o", "name": "GPT-4o"},
        {"id": "gpt-4o-mini", "name": "GPT-4o-mini"}
    ],
    "antigravity": [
        {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
        {"id": "gemini-3.7-flash", "name": "Gemini 3.7 Flash"},
        {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash"},
        {"id": "gemini-3.1-pro", "name": "Gemini 3.1 Pro"},
        {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
        {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
        {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6"},
        {"id": "gpt-oss-120b", "name": "GPT-OSS 120B"}
    ],
    "all": [
        {"id": "auto", "name": "Auto"}
    ]
}

EFFORTS_BY_PROVIDER: Dict[str, List[Dict[str, str]]] = {
    "claude_code": [
        {"id": "default", "name": "Default"},
        {"id": "low", "name": "Low Effort"},
        {"id": "medium", "name": "Medium Effort"},
        {"id": "high", "name": "High Effort"},
        {"id": "xhigh", "name": "Extra High Effort"},
        {"id": "max", "name": "Maximum Effort"}
    ],
    "codex": [
        {"id": "default", "name": "Default"},
        {"id": "low", "name": "Low Reasoning"},
        {"id": "medium", "name": "Medium Reasoning"},
        {"id": "high", "name": "High Reasoning"},
        {"id": "max", "name": "Maximum Reasoning"}
    ],
    "antigravity": [
        {"id": "default", "name": "Default"},
        {"id": "low", "name": "Low Reasoning"},
        {"id": "medium", "name": "Medium Reasoning"},
        {"id": "high", "name": "High Reasoning"}
    ],
    "all": [
        {"id": "default", "name": "Auto"},
        {"id": "high", "name": "High Reasoning"}
    ]
}


# Which entry of MODELS and EFFORTS_BY_PROVIDER an agent name selects. A name
# may carry an instance suffix after "@", for example "codex@laptop".
_PROVIDER_ALIASES = {
    "claude": "claude_code",
    "claude_code": "claude_code",
    "codex": "codex",
    "chatgpt": "codex",
    "antigravity": "antigravity",
    "agy": "antigravity",
    "all": "all",
}

# Values the runner treats as "not set". They are always accepted.
NEUTRAL_MODELS = frozenset({"auto"})
NEUTRAL_EFFORTS = frozenset({"default"})


class TaskOptionError(ValueError):
    """A task asks for a model or effort its agent does not offer."""


def provider_for_agent(agent: str) -> Optional[str]:
    base = (agent or "").split("@", 1)[0].strip().lower()
    return _PROVIDER_ALIASES.get(base)


def validate_task_options(
    task_id: str,
    agent: str,
    model: Optional[str],
    effort: Optional[str],
    subject: Optional[str] = None,
) -> None:
    """Raise TaskOptionError unless model and effort are on the agent's lists.

    An agent with no list (a custom adapter, a web LLM) accepts neither, since
    there is nothing to check the value against. `subject` names what is being
    checked in the message and defaults to the task.
    """
    if model is None and effort is None:
        return
    label = subject or f"Task '{task_id}'"
    provider = provider_for_agent(agent)
    if model is not None:
        allowed = {m["id"] for m in MODELS.get(provider or "", [])} | NEUTRAL_MODELS
        if provider is None or model not in allowed:
            raise TaskOptionError(f"{label} asks for model '{model[:64]}', which agent '{agent}' does not offer.")
    if effort is not None:
        allowed = {e["id"] for e in EFFORTS_BY_PROVIDER.get(provider or "", [])} | NEUTRAL_EFFORTS
        if provider is None or effort not in allowed:
            raise TaskOptionError(f"{label} asks for effort '{effort[:64]}', which agent '{agent}' does not offer.")
