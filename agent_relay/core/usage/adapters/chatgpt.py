"""ChatGPT and Codex usage.

The top rung reads the usage windows ChatGPT publishes for the Codex sign-in on
this machine. That call was already correct in substance. The one defect it had
was formatting ``reset_after_seconds`` into a countdown string at read time and
storing that, which froze. Here the offset is converted into an absolute
instant, and the countdown is produced at render time instead.
"""

import json
from datetime import timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from ..base import AccountLike, SourceFn, UsageAdapter
from ..models import (
    CONFIDENCE_MEASURED,
    UNIT_PERCENT,
    UNIT_REQUESTS,
    UNIT_TOKENS,
    PlanInfo,
    UsageObservation,
    UsageWindow,
    WINDOW_RATE_LIMIT,
    WINDOW_SESSION,
    WINDOW_WEEK,
    utc_now,
)

CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
WHAM_URL = "https://chatgpt.com/backend-api/wham/usage"
REFRESH_SECONDS = 300


def _as_float(value) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window_end_from_offset(seconds) -> Optional[object]:
    """Convert a "resets in N seconds" offset into an absolute instant.

    This is the whole fix for this provider. An offset is only meaningful at the
    moment it is read, so it is anchored to that moment immediately.
    """
    value = _as_float(seconds)
    if value is None or value < 0:
        return None
    return utc_now() + timedelta(seconds=value)


def _read_codex_auth() -> tuple:
    if not CODEX_AUTH_PATH.exists():
        return None, None
    try:
        data = json.loads(CODEX_AUTH_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    tokens = data.get("tokens") or {}
    return tokens.get("access_token"), tokens.get("account_id")


def fetch_codex_auth(account: AccountLike) -> Optional[UsageObservation]:
    """Read the Codex five-hour and weekly windows ChatGPT reports."""
    cred = (account.credential or "").strip()
    access_token, account_id = _read_codex_auth()
    token = cred if (cred.startswith("eyJ") and len(cred) > 50) else access_token
    if not token:
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    if account_id:
        headers["ChatGPT-Account-ID"] = account_id

    try:
        with httpx.Client(timeout=12.0) as client:
            res = client.get(WHAM_URL, headers=headers)
    except Exception as exc:
        return UsageObservation.unavailable(
            "codex_auth",
            f"Could not reach the ChatGPT usage API: {exc}",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    if res.status_code in (401, 403):
        return UsageObservation.unavailable(
            "codex_auth",
            "ChatGPT refused the Codex sign-in on this machine. Sign in to Codex "
            "again to refresh it.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )
    if res.status_code != 200:
        return UsageObservation.unavailable(
            "codex_auth",
            f"The ChatGPT usage API returned status {res.status_code}.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    try:
        data = res.json()
    except ValueError:
        return UsageObservation.unavailable(
            "codex_auth",
            "The ChatGPT usage API returned no usage data.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    plan_type = (data.get("plan_type") or "").strip()
    plan = PlanInfo(
        name=f"ChatGPT {plan_type.title()}" if plan_type else None,
        label=plan_type.title() or None,
    )

    rate_limit = data.get("rate_limit") or {}
    primary = rate_limit.get("primary_window") or {}
    secondary = rate_limit.get("secondary_window") or {}
    if not primary and not secondary:
        return UsageObservation.unavailable(
            "codex_auth",
            "ChatGPT reported no rate-limit windows for this account.",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    windows: List[UsageWindow] = []
    primary_used = _as_float(primary.get("used_percent"))
    if primary_used is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_SESSION,
                label="Session, 5 hours",
                unit=UNIT_PERCENT,
                used=primary_used,
                limit=100.0,
                window_end=_window_end_from_offset(primary.get("reset_after_seconds")),
                is_active=True,
            )
        )
    secondary_used = _as_float(secondary.get("used_percent"))
    if secondary_used is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_WEEK,
                label="Weekly",
                unit=UNIT_PERCENT,
                used=secondary_used,
                limit=100.0,
                window_end=_window_end_from_offset(secondary.get("reset_after_seconds")),
            )
        )

    if not windows:
        return UsageObservation.unavailable(
            "codex_auth",
            "ChatGPT reported windows with no usage share, so there is no figure "
            "to show.",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    return UsageObservation(
        source="codex_auth",
        confidence=CONFIDENCE_MEASURED,
        windows=windows,
        plan=plan,
        expected_refresh_seconds=REFRESH_SECONDS,
    )


def _header_int(headers, name: str) -> Optional[int]:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fetch_api_key(account: AccountLike) -> Optional[UsageObservation]:
    """Read the per-minute rate limit an OpenAI API key is subject to."""
    cred = (account.credential or "").strip()
    if not cred.startswith("sk-") or cred.startswith("sk-ant-"):
        return None

    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {cred}"},
            )
    except Exception as exc:
        return UsageObservation.unavailable(
            "openai_api_key", f"Could not reach the OpenAI API: {exc}"
        )

    if res.status_code == 401:
        return UsageObservation.unavailable("openai_api_key", "Invalid OpenAI API key.")
    if res.status_code != 200:
        return UsageObservation.unavailable(
            "openai_api_key", f"The OpenAI API returned status {res.status_code}."
        )

    plan = PlanInfo(name="OpenAI API", label="API key")
    tok_limit = _header_int(res.headers, "x-ratelimit-limit-tokens")
    tok_rem = _header_int(res.headers, "x-ratelimit-remaining-tokens")
    req_limit = _header_int(res.headers, "x-ratelimit-limit-requests")
    req_rem = _header_int(res.headers, "x-ratelimit-remaining-requests")

    windows: List[UsageWindow] = []
    if tok_limit is not None and tok_rem is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_RATE_LIMIT,
                label="API tokens per minute",
                unit=UNIT_TOKENS,
                used=float(max(0, tok_limit - tok_rem)),
                limit=float(tok_limit),
            )
        )
    if req_limit is not None and req_rem is not None:
        windows.append(
            UsageWindow(
                key="rate_limit_requests",
                label="API requests per minute",
                unit=UNIT_REQUESTS,
                used=float(max(0, req_limit - req_rem)),
                limit=float(req_limit),
            )
        )

    if not windows:
        return UsageObservation.unavailable(
            "openai_api_key",
            "The OpenAI API key is valid but returned no rate-limit headers, so "
            "there is no usage figure to show.",
            plan=plan,
        )

    return UsageObservation(
        source="openai_api_key",
        confidence=CONFIDENCE_MEASURED,
        windows=windows,
        plan=plan,
        expected_refresh_seconds=60,
    )


def fetch_nothing(account: AccountLike) -> Optional[UsageObservation]:
    """Last rung, so the card says why rather than showing a blank."""
    return UsageObservation.unavailable(
        "codex_auth",
        f"No Codex sign-in was found at {CODEX_AUTH_PATH} and this account "
        "carries no OpenAI API key, so there is nothing to read usage from.",
        expected_refresh_seconds=REFRESH_SECONDS,
    )


class ChatGPTAdapter(UsageAdapter):
    provider = "chatgpt"
    display_name = "ChatGPT (OpenAI)"
    hint = "Codex sign-in, or an sk- API key"

    def sources(self) -> List[Tuple[str, SourceFn]]:
        return [
            ("codex_auth", fetch_codex_auth),
            ("openai_api_key", fetch_api_key),
            ("none", fetch_nothing),
        ]
