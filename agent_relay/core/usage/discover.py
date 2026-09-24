"""Find the agent sign-ins already on this machine and describe them.

Every adapter reads a tool's own local sign-in and needs no credential pasted
in, so there is nothing for an operator to configure: if Claude Code is signed
in on this machine, its usage is readable, and the only reason the Usage tab
would be empty is that nobody had clicked Add Account yet.

That is what this module removes. The hub seeds an account for each tool it
finds on first run, so a fresh install shows correct usage without being set
up, and a tool installed later is picked up the next time discovery runs.

Detection is deliberately shallow. It asks only "has this tool left a sign-in
here", never "how much quota is left", because the adapter answers that and a
guess here would be exactly the kind of invented figure this rebuild removed.
"""

import json
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# The adapter modules are held, not their path constants. Each adapter resolves
# its credential location as a module-level constant, so importing the constant
# here would bind a copy and stop following the adapter: a test that redirects
# an adapter at a temporary home, or any future code that relocates a path,
# would be invisible to discovery.
from .adapters import antigravity as _agy
from .adapters import chatgpt as _chatgpt_adapter
from .adapters import claude as _claude_adapter
from .adapters import google_code_assist as _google


def _claude() -> Optional[Dict[str, str]]:
    oauth = _claude_adapter.read_claude_oauth()
    if not oauth:
        return None
    subscription = (oauth.get("subscriptionType") or "").strip()
    label = f"Claude {subscription.title()}" if subscription else "Claude Code"
    return {
        "provider": "claude",
        "name": label,
        "plan_name": label,
        "detected_from": str(_claude_adapter.CREDENTIALS_PATH),
    }


def _chatgpt() -> Optional[Dict[str, str]]:
    if not _chatgpt_adapter.CODEX_AUTH_PATH.exists():
        return None
    return {
        "provider": "chatgpt",
        "name": "ChatGPT (Codex)",
        "plan_name": "ChatGPT",
        "detected_from": str(_chatgpt_adapter.CODEX_AUTH_PATH),
    }


def _antigravity_installed() -> Optional[Path]:
    """Where AntiGravity is installed or has left its data, or None.

    Its log only exists once the app has run a session, so a machine where
    AntiGravity was installed and signed in, but never used for a chat, was
    not detected at all.
    """
    candidates = [Path.home() / ".gemini" / "antigravity"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.insert(0, Path(local) / "Programs" / "antigravity")
    return next((path for path in candidates if path.exists()), None)


def _gemini() -> Optional[Dict[str, str]]:
    if not _google.CREDS_PATH.exists():
        # The Gemini figures now come from AntiGravity's sign-in, so a machine
        # with AntiGravity and no Gemini CLI still has a Gemini card to show.
        installed = _antigravity_installed()
        if installed is None:
            return None
        return {
            "provider": "gemini",
            "name": "Gemini",
            "plan_name": "Gemini",
            "detected_from": str(installed),
        }
    email = _google.active_google_account()
    return {
        "provider": "gemini",
        "name": f"Gemini ({email})" if email else "Gemini",
        "plan_name": "Gemini",
        "detected_from": str(_google.ACCOUNTS_PATH if email else _google.CREDS_PATH),
    }


def _antigravity() -> Optional[Dict[str, str]]:
    log = next(
        (path for path in (_agy.CLI_LOG, _agy.DESKTOP_LOG) if path.exists()), None
    ) or _antigravity_installed()
    if log is None:
        return None
    return {
        "provider": "antigravity",
        "name": "AntiGravity",
        "plan_name": "AntiGravity",
        "detected_from": str(log),
    }


# In registry order, so a seeded dashboard lays out the same way as a
# hand-built one. DeepSeek and a custom harness are absent on purpose: both
# need a key or a URL that only the operator can supply, so there is nothing to
# detect.
_DETECTORS = (_claude, _chatgpt, _gemini, _antigravity)


def discover_local_accounts() -> List[Dict[str, str]]:
    """Every agent tool with a sign-in on this machine, in registry order."""
    found: List[Dict[str, str]] = []
    for detector in _DETECTORS:
        try:
            result = detector()
        except Exception:
            # A malformed credential file must not stop the other tools being
            # found. The adapter reports the problem when the card is read.
            continue
        if result:
            found.append(result)
    return found


def missing_providers(existing_providers, skip: Iterable[str] = ()) -> List[Dict[str, str]]:
    """Discovered tools that have no account yet.

    Used both to seed a fresh install and to pick up a tool installed later,
    without ever duplicating an account the operator already has. ``skip``
    names providers the operator removed on purpose.
    """
    have = {(p or "").strip().lower() for p in existing_providers}
    have |= {(p or "").strip().lower() for p in skip}
    return [found for found in discover_local_accounts() if found["provider"] not in have]


# ---------------------------------------------------------------------------
# Providers removed on purpose
# ---------------------------------------------------------------------------
#
# Discovery runs at every start and every few minutes, so a tool signed in
# after the first start still gets its card. A card the operator deleted must
# not come back behind their back, so deleting the last account of a provider
# records it here, and adding one by hand clears it again.

DISMISSED_PATH = Path.home() / ".agnview" / "usage_dismissed.json"


def dismissed_providers() -> List[str]:
    try:
        data = json.loads(DISMISSED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def _save_dismissed(providers: Iterable[str]) -> None:
    DISMISSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    DISMISSED_PATH.write_text(json.dumps(sorted(set(providers))), encoding="utf-8")


def dismiss_provider(provider: str) -> None:
    key = (provider or "").strip().lower()
    if key:
        _save_dismissed(set(dismissed_providers()) | {key})


def undismiss_provider(provider: str) -> None:
    key = (provider or "").strip().lower()
    current = set(dismissed_providers())
    if key in current:
        _save_dismissed(current - {key})
