"""Gemini usage.

Google publishes no local quota file for Gemini. The one source that reports
anything about the signed-in account is Code Assist, shared with the AntiGravity
adapter. An AI Studio API key can be checked for validity and nothing more, so
that rung reports unavailable with the reason rather than a zero.
"""

from typing import List, Optional, Tuple

import httpx

from ..base import AccountLike, SourceFn, UsageAdapter
from ..models import PlanInfo, UsageObservation
from .google_code_assist import REFRESH_SECONDS, fetch_code_assist


def fetch_tier(account: AccountLike) -> Optional[UsageObservation]:
    cred = (account.credential or "").strip()
    if cred.startswith("AIza"):
        # This account is an API key, not the CLI sign-in. Let the next rung
        # handle it rather than reporting a different account's tier.
        return None
    return fetch_code_assist("gemini_code_assist", "Gemini", "GEMINI")


def fetch_api_key(account: AccountLike) -> Optional[UsageObservation]:
    """Confirm an AI Studio key works. Google attaches no quota figure to it."""
    cred = (account.credential or "").strip()
    if not cred.startswith("AIza"):
        return None

    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": cred},
            )
    except Exception as exc:
        return UsageObservation.unavailable(
            "gemini_api_key", f"Could not reach the Gemini API: {exc}"
        )

    if res.status_code in (400, 403):
        return UsageObservation.unavailable(
            "gemini_api_key", "Invalid Google Gemini API key."
        )
    if res.status_code != 200:
        return UsageObservation.unavailable(
            "gemini_api_key", f"The Gemini API returned status {res.status_code}."
        )

    return UsageObservation.unavailable(
        "gemini_api_key",
        "The Gemini API key is valid. Google publishes no quota figure for it, so "
        "there is no usage to show.",
        plan=PlanInfo(name="Gemini AI Studio", label="API key"),
        expected_refresh_seconds=REFRESH_SECONDS,
    )


class GeminiAdapter(UsageAdapter):
    provider = "gemini"
    display_name = "Google Gemini"
    hint = "Gemini CLI sign-in, or an AIza API key"

    def sources(self) -> List[Tuple[str, SourceFn]]:
        return [
            ("gemini_code_assist", fetch_tier),
            ("gemini_api_key", fetch_api_key),
        ]
