"""The Google Code Assist source, shared by Gemini and AntiGravity.

Both tools sign in with the same Google account and both call
``v1internal:loadCodeAssist`` on ``cloudcode-pa.googleapis.com``. AntiGravity's
own log shows that call, which is why one source module serves both adapters
rather than each guessing separately.

The route is confirmed to exist: a POST with no credential returns 401, not 404.
Its response shape is not confirmed, because the Google OAuth token on the
machine this was written against had expired and AgnView does not refresh
tokens it did not mint. The parsing below is therefore defensive: it reads the
tier when it can find it, reports a quota only when the response actually
carries one, and otherwise says plainly that Google named a tier and no figure.
It never derives a percentage from a limit it did not receive.
"""

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from ..models import (
    CONFIDENCE_MEASURED,
    UNIT_REQUESTS,
    PlanInfo,
    UsageObservation,
    UsageWindow,
    WINDOW_SESSION,
    parse_instant,
)

CREDS_PATH = Path.home() / ".gemini" / "oauth_creds.json"
ACCOUNTS_PATH = Path.home() / ".gemini" / "google_accounts.json"
LOAD_URL = "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
REFRESH_SECONDS = 600


def read_google_oauth() -> Optional[Dict[str, Any]]:
    if not CREDS_PATH.exists():
        return None
    try:
        data = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def active_google_account() -> Optional[str]:
    if not ACCOUNTS_PATH.exists():
        return None
    try:
        data = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    active = data.get("active")
    return active if isinstance(active, str) and active.strip() else None


def _tier_label(tier: Any) -> Optional[str]:
    if not isinstance(tier, dict):
        return None
    return (
        (tier.get("name") or "").strip()
        or (tier.get("displayName") or "").strip()
        or (tier.get("tierName") or "").strip()
        or (tier.get("id") or "").strip()
        or (tier.get("tierId") or "").strip()
        or None
    )


def _tier_plan(payload: Dict[str, Any], product: str) -> PlanInfo:
    """Name the plan from whichever tier field the response carries.

    An account that has never picked a tier carries no ``currentTier`` at all.
    The response for one, confirmed live on 2026-09-22, holds only
    ``allowedTiers`` and ``ineligibleTiers``, so the default allowed tier is
    named instead of leaving the card with no plan at all.
    """
    label = _tier_label(payload.get("currentTier"))
    if label:
        return PlanInfo(name=product, label=label)

    allowed = payload.get("allowedTiers")
    if isinstance(allowed, list):
        for tier in allowed:
            if isinstance(tier, dict) and tier.get("isDefault"):
                found = _tier_label(tier)
                if found:
                    return PlanInfo(name=product, label=f"{found} (available)")
        for tier in allowed:
            found = _tier_label(tier)
            if found:
                return PlanInfo(name=product, label=f"{found} (available)")

    return PlanInfo(name=product, label=None)


def _ineligible_reason(payload: Dict[str, Any]) -> Optional[str]:
    """Google's own explanation when this client cannot use Code Assist.

    Worth surfacing verbatim rather than flattening into "no quota figure".
    Google currently answers a personal Gemini CLI sign-in with
    ``UNSUPPORTED_CLIENT`` and the text "This client is no longer supported for
    Gemini Code Assist for individuals ... migrate to the Antigravity suite",
    which is the actual reason the card is empty and tells the operator what to
    do about it.
    """
    ineligible = payload.get("ineligibleTiers")
    if not isinstance(ineligible, list):
        return None
    for entry in ineligible:
        if not isinstance(entry, dict):
            continue
        message = (entry.get("reasonMessage") or "").strip()
        if message:
            tier = (entry.get("tierName") or entry.get("tierId") or "").strip()
            return f"{message} (tier: {tier})" if tier else message
    return None


# Quota can appear under several names across Code Assist revisions. Each is
# checked, and a figure is reported only when both a used count and a limit are
# genuinely present. A limit on its own is a denominator with no numerator, and
# reporting it as usage would be an invented figure.
_QUOTA_CONTAINERS = ("quota", "userQuota", "quotas", "usageQuota", "rateLimit")
_USED_KEYS = ("used", "usedCount", "consumed", "requestsUsed", "count")
_LIMIT_KEYS = ("limit", "maxCount", "quotaLimit", "requestsLimit", "total")
_RESET_KEYS = ("resetTime", "resetsAt", "nextResetTime", "expireTime")


def _first_number(block: Dict[str, Any], keys) -> Optional[float]:
    for key in keys:
        value = block.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _first_instant(block: Dict[str, Any], keys):
    for key in keys:
        found = parse_instant(block.get(key))
        if found is not None:
            return found
    return None


def _quota_windows(payload: Dict[str, Any]) -> List[UsageWindow]:
    """Every quota block the response carries that is complete enough to show."""
    windows: List[UsageWindow] = []

    def consume(block: Any, label_hint: str) -> None:
        if not isinstance(block, dict):
            return
        used = _first_number(block, _USED_KEYS)
        limit = _first_number(block, _LIMIT_KEYS)
        if used is None or not limit:
            return
        label = (
            (block.get("displayName") or "").strip()
            or (block.get("name") or "").strip()
            or label_hint
        )
        windows.append(
            UsageWindow(
                key=WINDOW_SESSION if not windows else f"quota_{len(windows)}",
                label=label,
                unit=UNIT_REQUESTS,
                used=used,
                limit=limit,
                window_end=_first_instant(block, _RESET_KEYS),
            )
        )

    for container in _QUOTA_CONTAINERS:
        block = payload.get(container)
        if isinstance(block, dict):
            consume(block, "Quota")
        elif isinstance(block, list):
            for index, entry in enumerate(block):
                consume(entry, f"Quota {index + 1}")

    return windows


def fetch_code_assist(
    source_id: str, product: str, plugin_type: str
) -> Optional[UsageObservation]:
    """Read the Code Assist tier, and a quota when Google reports one.

    ``source_id`` is the rung name recorded on the observation, ``product`` is
    what the card calls the tool, and ``plugin_type`` is what Google is told
    this client is.
    """
    oauth = read_google_oauth()
    email = active_google_account()

    if not oauth or not (oauth.get("access_token") or "").strip():
        return UsageObservation.unavailable(
            source_id,
            f"No Google sign-in was found at {CREDS_PATH}. Sign in to {product} "
            "to create it.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    expiry = oauth.get("expiry_date")
    if isinstance(expiry, (int, float)) and expiry / 1000.0 <= time.time():
        signed_in = f" Signed in as {email}." if email else ""
        return UsageObservation.unavailable(
            source_id,
            f"The Google access token has expired.{signed_in} Start {product} to "
            "refresh it. AgnView does not refresh it, because writing that file "
            "would race the tool that owns it.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    headers = {
        "Authorization": f"Bearer {oauth['access_token']}",
        "Content-Type": "application/json",
    }
    body = {"metadata": {"pluginType": plugin_type}}
    try:
        with httpx.Client(timeout=15.0) as client:
            res = client.post(LOAD_URL, headers=headers, json=body)
    except Exception as exc:
        return UsageObservation.unavailable(
            source_id,
            f"Could not reach Google Code Assist: {exc}",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    if res.status_code in (401, 403):
        return UsageObservation.unavailable(
            source_id,
            f"Google refused the {product} sign-in on this machine. Start "
            f"{product} to refresh it.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )
    if res.status_code != 200:
        return UsageObservation.unavailable(
            source_id,
            f"Google Code Assist returned status {res.status_code}.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    try:
        payload = res.json()
    except ValueError:
        return UsageObservation.unavailable(
            source_id,
            "Google Code Assist returned no readable response.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )
    if not isinstance(payload, dict):
        return UsageObservation.unavailable(
            source_id,
            "Google Code Assist returned an unexpected response.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    plan = _tier_plan(payload, product)
    windows = _quota_windows(payload)

    if not windows:
        # Google's own ineligibility text beats a generic "no figure" message,
        # because it names the cause and what to do next.
        refused = _ineligible_reason(payload)
        if refused:
            return UsageObservation.unavailable(
                source_id,
                f"Google will not serve Code Assist to {product} on this "
                f"machine, so there is no quota to report. Google says: {refused}",
                plan=plan,
                expected_refresh_seconds=REFRESH_SECONDS,
            )
        tier_text = f" on the {plan.label} tier" if plan.label else ""
        return UsageObservation.unavailable(
            source_id,
            f"{product} is signed in{tier_text}. Google publishes no quota figure "
            "with it, so there is no usage to show.",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    return UsageObservation(
        source=source_id,
        confidence=CONFIDENCE_MEASURED,
        windows=windows,
        plan=plan,
        expected_refresh_seconds=REFRESH_SECONDS,
        notes={"tier": plan.label} if plan.label else {},
    )
