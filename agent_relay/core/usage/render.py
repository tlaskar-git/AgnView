"""Turn observations into what a card shows.

Every human-facing string in the Usage tab is produced here, from an
observation, at the moment of the request. Nothing this module returns is ever
written back to the database.

That separation is the fix for the defect that made the Claude card wrong: a
countdown stored at sync time froze, and there was no instant left behind to
recompute it from. A countdown produced here is a subtraction against
``window_end`` and is correct whenever it runs.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from .models import (
    CONFIDENCE_UNAVAILABLE,
    UNIT_CURRENCY,
    UNIT_PERCENT,
    UNIT_REQUESTS,
    UNIT_TOKENS,
    UsageObservation,
    UsageWindow,
    utc_now,
)


def format_countdown(window_end: Optional[datetime], now: datetime) -> Optional[str]:
    """Time left in a window, or None when the source gave no end instant.

    Returns None rather than a guess when there is nothing to subtract from.
    A card with no countdown is correct; a card with a made-up one is not.
    """
    if window_end is None:
        return None
    remaining = (window_end - now).total_seconds()
    if remaining <= 0:
        # The window closed. The next read will carry the new one, and until
        # then saying so is better than showing a negative countdown.
        return "Window closed, awaiting refresh"
    days, rem = divmod(int(remaining), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"Resets in {days}d {hours}h"
    if hours:
        return f"Resets in {hours}h {minutes}m"
    return f"Resets in {minutes}m"


def format_age(seconds: float) -> str:
    """How long ago a measurement was taken, always shown on the card."""
    total = int(max(0.0, seconds))
    if total < 60:
        return "just measured" if total < 10 else f"measured {total}s ago"
    minutes, _ = divmod(total, 60)
    if minutes < 60:
        return f"measured {minutes}m ago"
    hours, rem_min = divmod(minutes, 60)
    if hours < 24:
        return f"measured {hours}h {rem_min}m ago"
    days, rem_hours = divmod(hours, 24)
    return f"measured {days}d {rem_hours}h ago"


def format_amount(window: UsageWindow) -> Optional[str]:
    """The window's value in its own unit, or None when nothing was measured.

    A window carries exactly one unit, so this never mixes a percentage with a
    token count. Fusing those two was what produced a card reading both
    "2% used" and "370,216,622 tokens", which described different things over
    different scopes.
    """
    if window.used is None:
        return None
    if window.unit == UNIT_PERCENT:
        return f"{window.used:.0f}% used"
    if window.unit == UNIT_TOKENS:
        return f"{int(window.used):,} tokens"
    if window.unit == UNIT_REQUESTS:
        if window.limit:
            return f"{int(window.used):,} of {int(window.limit):,} requests"
        return f"{int(window.used):,} requests"
    if window.unit == UNIT_CURRENCY:
        code = window.currency or "USD"
        return f"{code} {window.used:,.2f}"
    return str(window.used)


def render_window(window: UsageWindow, now: datetime) -> Dict[str, Any]:
    """One window as a card row."""
    return {
        "key": window.key,
        "label": window.label,
        "sub_label": window.sub_label,
        "unit": window.unit,
        "amount_text": format_amount(window),
        # None when the source gave no denominator, and the UI then draws no
        # bar. It must not substitute zero.
        "percent_used": window.percent_used,
        "has_bar": window.has_bar,
        "used": window.used,
        "limit": window.limit,
        "severity": window.severity,
        "is_active": window.is_active,
        "window_start": window.window_start.isoformat() if window.window_start else None,
        "window_end": window.window_end.isoformat() if window.window_end else None,
        "countdown_text": format_countdown(window.window_end, now),
        "breakdown": [render_window(child, now) for child in window.breakdown],
    }


def render_observation(
    observation: Optional[UsageObservation], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """An observation as the Usage tab needs it.

    The age and the source are always present, so a figure cannot appear on the
    card without saying how old it is and where it came from.
    """
    now = now or utc_now()

    if observation is None:
        return {
            "source": "none",
            "source_label": "No source",
            "confidence": CONFIDENCE_UNAVAILABLE,
            "measured_at": None,
            "age_seconds": None,
            "age_text": "never measured",
            "is_stale": True,
            "error": "This account has not been read yet.",
            "plan_name": None,
            "plan_label": None,
            "windows": [],
        }

    age = observation.age_seconds(now)
    return {
        "source": observation.source,
        "source_label": observation.source_label,
        "confidence": observation.confidence,
        "measured_at": observation.measured_at.isoformat(),
        "age_seconds": round(age, 1),
        "age_text": format_age(age),
        "is_stale": observation.is_stale(now),
        "error": observation.error,
        "plan_name": observation.plan.name if observation.plan else None,
        "plan_label": observation.plan.label if observation.plan else None,
        "windows": [render_window(w, now) for w in observation.windows],
    }


# ---------------------------------------------------------------------------
# Legacy projection
# ---------------------------------------------------------------------------

# The Usage tab reads a flat set of fields. Until it reads observations
# directly, they are produced here on every request rather than stored. That
# keeps the old front end working without reintroducing the defect, because a
# countdown projected at read time cannot be stale.
_LEGACY_WINDOW_KEYS = {"session": ("session", "rate_limit", "balance"), "weekly": ("week",)}


def _first_window(observation: UsageObservation, keys) -> Optional[UsageWindow]:
    for key in keys:
        found = observation.window(key)
        if found is not None:
            return found
    return None


def project_legacy_fields(
    observation: Optional[UsageObservation], now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Flat fields for the current front end, computed, never persisted."""
    now = now or utc_now()
    flat: Dict[str, Any] = {
        "plan_label": None,
        "session_title": None,
        "session_reset_time": None,
        "session_percent_used": None,
        "session_percent_left": None,
        "session_tokens_used": None,
        "weekly_title": None,
        "weekly_reset_time": None,
        "weekly_percent_used": None,
        "weekly_percent_left": None,
        "weekly_tokens_used": None,
        "weekly_breakdown": None,
        "percent_used": None,
        "reset_time": None,
        "tokens_used": None,
        "tokens_limit": None,
        "tokens_remaining": None,
        "requests_used": None,
        "requests_limit": None,
        "requests_remaining": None,
        "cost_used_usd": None,
    }
    if observation is None:
        return flat

    if observation.plan:
        flat["plan_label"] = observation.plan.label

    session = _first_window(observation, _LEGACY_WINDOW_KEYS["session"])
    weekly = _first_window(observation, _LEGACY_WINDOW_KEYS["weekly"])

    if session is not None:
        flat["session_title"] = session.label
        flat["session_reset_time"] = (
            format_countdown(session.window_end, now) or session.sub_label
        )
        pct = session.percent_used
        if pct is not None:
            flat["session_percent_used"] = pct
            flat["session_percent_left"] = round(100.0 - pct, 1)
            flat["percent_used"] = pct
        if session.unit == UNIT_TOKENS and session.used is not None:
            flat["session_tokens_used"] = int(session.used)
            flat["tokens_used"] = int(session.used)
        if session.unit == UNIT_REQUESTS:
            if session.used is not None:
                flat["requests_used"] = int(session.used)
            if session.limit is not None:
                flat["requests_limit"] = int(session.limit)
                if session.used is not None:
                    flat["requests_remaining"] = max(0, int(session.limit) - int(session.used))
        if session.unit == UNIT_CURRENCY and session.used is not None:
            flat["cost_used_usd"] = round(float(session.used), 2)
        flat["reset_time"] = flat["session_reset_time"]

    if weekly is not None:
        flat["weekly_title"] = weekly.label
        flat["weekly_reset_time"] = (
            format_countdown(weekly.window_end, now) or weekly.sub_label
        )
        pct = weekly.percent_used
        if pct is not None:
            flat["weekly_percent_used"] = pct
            flat["weekly_percent_left"] = round(100.0 - pct, 1)
        if weekly.unit == UNIT_TOKENS and weekly.used is not None:
            flat["weekly_tokens_used"] = int(weekly.used)
        if weekly.breakdown:
            flat["weekly_breakdown"] = [
                {
                    "label": child.label,
                    "percent_used": child.percent_used,
                    "reset_time": format_countdown(child.window_end, now),
                }
                for child in weekly.breakdown
            ]

    return flat


def legacy_status(observation: Optional[UsageObservation]) -> str:
    """The old status string, derived rather than stored.

    Nothing re-asserts "active" over a failed read any more, which is what let
    a stale synced percentage present itself as a live measurement.
    """
    if observation is None:
        return "unknown"
    if observation.confidence == CONFIDENCE_UNAVAILABLE:
        return "unavailable" if observation.error else "unknown"
    worst = "active"
    for window in observation.windows:
        if window.severity in ("exhausted", "critical"):
            return "exhausted"
        if window.severity == "warning":
            worst = "warning"
    return worst


def all_window_keys(observations: List[UsageObservation]) -> List[str]:
    """Every distinct window key across observations, for the UI to lay out."""
    seen: List[str] = []
    for observation in observations:
        for window in observation.windows:
            if window.key not in seen:
                seen.append(window.key)
    return seen
