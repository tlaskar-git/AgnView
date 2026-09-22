"""The AntiGravity usage panel, read through the app's own debug port.

This is the highest rung for both Gemini and AntiGravity, and it is the only
source on the machine that produces a real percentage for either. AntiGravity
prints both its own limits and the Gemini model limits in its model picker, and
the Electron app exposes that window on a loopback debug port.

The reading itself lives in ``agent_relay.core.antigravity``. This module only
turns the payload it returns into an observation, so the panel's parsing rules
stay in one place.

Two conditions, both reported rather than worked around:

- The app has to be running. Nothing here starts it, because a usage refresh
  that launched an application would be intrusive.
- Its usage panel has to be on screen, because the read is of text already
  displayed. That is the instruction an operator needs, so it is passed through
  verbatim.

The panel prints a reset for the windows that have one, as "Resets in 2d 14h".
The reader carries that as an offset in seconds so its two implementations stay
comparable, and it is anchored to an absolute instant here, at the moment of the
read. A window the panel showed no reset for carries no ``window_end`` and draws
no countdown, which is correct: inventing one would be the defect this rebuild
removed.
"""

from datetime import timedelta
from typing import List, Optional

# The module is held, not the function. Binding read_usage here would take a
# copy and stop following the reader, so a test that substitutes it, or any
# future change to how it is dispatched, would be invisible from here.
from ... import antigravity as _reader
from ..models import (
    CONFIDENCE_MEASURED,
    UNIT_PERCENT,
    PlanInfo,
    UsageObservation,
    UsageWindow,
    WINDOW_SESSION,
    WINDOW_WEEK,
    utc_now,
)

# The panel is read from a live window, so it is current whenever the app is
# open. Re-read often enough that a card follows the app, cheaply: this costs
# one loopback websocket round trip and no provider request.
REFRESH_SECONDS = 120


def _sub_label(title, parent_label: str) -> Optional[str]:
    """A row's own title, unless it merely repeats the window above it."""
    text = (title or "").strip()
    if not text or text.casefold() == (parent_label or "").strip().casefold():
        return None
    return text


def _window_end(resets_in_seconds):
    """Anchor a "resets in N seconds" offset to an instant, or None.

    An offset only means anything at the moment it was read, so it is pinned
    immediately. Everything downstream works from the instant.
    """
    if resets_in_seconds is None:
        return None
    try:
        seconds = int(resets_in_seconds)
    except (TypeError, ValueError):
        return None
    return utc_now() + timedelta(seconds=seconds) if seconds >= 0 else None


def _rows_to_breakdown(
    rows, prefix: str, used_key: str, title_key: str, reset_key: str, parent_label: str
) -> List[UsageWindow]:
    """One window per model group, as the panel listed them.

    ``parent_label`` is the window these rows sit under. Every row in the panel
    carries the same window title as its parent, so repeating it on each row put
    "Five Hour Limit Remaining" under a heading already reading exactly that.
    The title is kept only on a row that says something different.
    """
    children: List[UsageWindow] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        used = row.get(used_key)
        group = (row.get("group") or "").strip()
        if used is None or not group:
            continue
        try:
            value = float(used)
        except (TypeError, ValueError):
            continue
        children.append(
            UsageWindow(
                key=f"{prefix}_{group.lower().replace(' ', '_')}",
                label=group,
                sub_label=_sub_label(row.get(title_key), parent_label),
                unit=UNIT_PERCENT,
                used=value,
                limit=100.0,
                window_end=_window_end(row.get(reset_key)),
            )
        )
    return children


def observation_from_payload(payload: dict, product: str) -> Optional[UsageObservation]:
    """Turn a panel payload into an observation, or None when it carries nothing."""
    windows: List[UsageWindow] = []

    session_used = payload.get("session_percent_used")
    session_label = (payload.get("session_title") or "Session limit").strip()
    if session_used is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_SESSION,
                label=session_label,
                unit=UNIT_PERCENT,
                used=float(session_used),
                limit=100.0,
                window_end=_window_end(payload.get("session_resets_in_seconds")),
                is_active=True,
                breakdown=_rows_to_breakdown(
                    payload.get("session_breakdown"),
                    "session_group",
                    "session_percent_used",
                    "session_title",
                    "session_resets_in_seconds",
                    session_label,
                ),
            )
        )

    weekly_used = payload.get("weekly_percent_used")
    weekly_label = (payload.get("weekly_title") or "Weekly limit").strip()
    if weekly_used is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_WEEK,
                label=weekly_label,
                unit=UNIT_PERCENT,
                used=float(weekly_used),
                limit=100.0,
                window_end=_window_end(payload.get("weekly_resets_in_seconds")),
                breakdown=_rows_to_breakdown(
                    payload.get("weekly_breakdown"),
                    "week_group",
                    "weekly_percent_used",
                    "weekly_title",
                    "weekly_resets_in_seconds",
                    weekly_label,
                ),
            )
        )

    if not windows:
        return None

    return UsageObservation(
        source="agy_panel",
        confidence=CONFIDENCE_MEASURED,
        windows=windows,
        plan=PlanInfo(name=product, label="Read from the AntiGravity app"),
        expected_refresh_seconds=REFRESH_SECONDS,
    )


def fetch_panel(product: str) -> Optional[UsageObservation]:
    """Read the panel. Unavailable with the app's own reason when it cannot.

    The reason matters more here than anywhere else in the ladder, because both
    failures are things the operator can fix in a second: open the app, or open
    the model picker.
    """
    read = _reader.read_usage()
    if read.payload:
        observation = observation_from_payload(read.payload, product)
        if observation is not None:
            return observation
        return UsageObservation.unavailable(
            "agy_panel",
            "AntiGravity's usage panel was read but carried no figure. Open the "
            "model picker so the limits are showing, then refresh.",
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    return UsageObservation.unavailable(
        "agy_panel",
        read.error or _reader.NOT_RUNNING_MESSAGE,
        expected_refresh_seconds=REFRESH_SECONDS,
    )
