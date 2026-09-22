"""The provider registry.

Dispatch is an exact key lookup. An unregistered provider raises, and the caller
turns that into a rejected account rather than a silent probe of something else.

Provider dispatch used to be a chain of substring tests ending in a fallthrough
to the local-harness adapter. Two defects came out of that shape. A provider
with no adapter was probed as an Ollama server without saying so, which is what
happened to AntiGravity. And the Add Account dropdown was a separate hand-written
list, so it drifted out of step with the adapters and omitted AntiGravity
entirely. The dropdown is now generated from this registry, so the two cannot
disagree.
"""

from typing import Dict, List

from .adapters.antigravity import AntiGravityAdapter
from .adapters.chatgpt import ChatGPTAdapter
from .adapters.claude import ClaudeAdapter
from .adapters.custom import CustomAdapter
from .adapters.deepseek import DeepSeekAdapter
from .adapters.gemini import GeminiAdapter
from .base import UsageAdapter


class UnknownProvider(ValueError):
    """Raised when an account names a provider with no adapter behind it."""


_ADAPTERS: List[UsageAdapter] = [
    ClaudeAdapter(),
    ChatGPTAdapter(),
    GeminiAdapter(),
    AntiGravityAdapter(),
    DeepSeekAdapter(),
    CustomAdapter(),
]

PROVIDERS: Dict[str, UsageAdapter] = {adapter.provider: adapter for adapter in _ADAPTERS}


def normalise(provider: str) -> str:
    return (provider or "").strip().lower()


def is_known(provider: str) -> bool:
    return normalise(provider) in PROVIDERS


def get_adapter(provider: str) -> UsageAdapter:
    key = normalise(provider)
    adapter = PROVIDERS.get(key)
    if adapter is None:
        known = ", ".join(sorted(PROVIDERS))
        raise UnknownProvider(
            f"'{provider}' is not a usage provider. Known providers: {known}."
        )
    return adapter


def provider_options() -> List[Dict[str, str]]:
    """The Add Account dropdown, in the order the registry declares."""
    return [
        {
            "value": adapter.provider,
            "label": adapter.display_name,
            "hint": adapter.hint,
        }
        for adapter in _ADAPTERS
    ]
