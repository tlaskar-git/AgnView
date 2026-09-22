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

The panel shows no reset time, so these windows carry no ``window_end`` and the
card draws no countdown for them. That is correct: inventing one would be the
same defect this rebuild removed.
"""

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
)

# The panel is read from a live window, so it is current whenever the app is
# open. Re-read often enough that a card follows the app, cheaply: this costs
# one loopback websocket round trip and no provider request.
REFRESH_SECONDS = 120


def _rows_to_breakdown(rows, prefix: str, used_key: str, title_key: str) -> List[UsageWindow]:
    """One window per model group, as the panel listed them."""
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
                sub_label=(row.get(title_key) or "").strip() or None,
                unit=UNIT_PERCENT,
                used=value,
                limit=100.0,
            )
        )
    return children


def observation_from_payload(payload: dict, product: str) -> Optional[UsageObservation]:
    """Turn a panel payload into an observation, or None when it carries nothing."""
    windows: List[UsageWindow] = []

    session_used = payload.get("session_percent_used")
    if session_used is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_SESSION,
                label=(payload.get("session_title") or "Session limit").strip(),
                unit=UNIT_PERCENT,
                used=float(session_used),
                limit=100.0,
                is_active=True,
                breakdown=_rows_to_breakdown(
                    payload.get("session_breakdown"),
                    "session_group",
                    "session_percent_used",
                    "session_title",
                ),
            )
        )

    weekly_used = payload.get("weekly_percent_used")
    if weekly_used is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_WEEK,
                label=(payload.get("weekly_title") or "Weekly limit").strip(),
                unit=UNIT_PERCENT,
                used=float(weekly_used),
                limit=100.0,
                breakdown=_rows_to_breakdown(
                    payload.get("weekly_breakdown"),
                    "week_group",
                    "weekly_percent_used",
                    "weekly_title",
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
