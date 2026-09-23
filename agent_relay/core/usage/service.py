"""The entry point the API calls.

One function fetches an observation for an account, and one decides whether a
stored observation is old enough to need refreshing. Nothing here merges an old
observation into a new one.

That last point is the fix for the worst defect in the previous design. It kept
a manually synced percentage for seven days and wrote it back over every fresh
read, re-marking the account active and clearing its error, so a figure measured
three days earlier was presented as current with its age shown nowhere. A read
now replaces the observation outright, with one exception, below.

When a read fails but the last real reading still describes open windows, the
card keeps that reading instead of dropping to a weaker source or a blank. It
keeps its original measured time, so its age shows on the card, it carries the
reason the newer read failed, and it is dropped the moment its windows reset.
A Claude or AntiGravity card used to fall back to a local token count, or to
"unavailable", every time a sign-in expired or the app closed, which is the
wrong information: the plan figures had not changed.
"""

from datetime import datetime
from typing import Optional

from .base import AccountLike
from .models import UsageObservation, utc_now
from .registry import get_adapter


HELD_NOTE = "held_last_reading"
CHECKED_NOTE = "checked_at"


def fetch_observation(
    account: AccountLike, previous: Optional[UsageObservation] = None
) -> UsageObservation:
    """Read this account from the best source its adapter can reach.

    ``previous`` is the stored observation. It is kept, marked as held, when
    the new read has no real figure and ``previous`` still does.

    Raises ``UnknownProvider`` when the account names a provider with no
    adapter. The caller turns that into a clear error rather than probing
    something unrelated.
    """
    adapter = get_adapter(account.provider)
    return hold_last_reading(adapter.fetch(account), previous)


def hold_last_reading(
    new: UsageObservation,
    previous: Optional[UsageObservation],
    now: Optional[datetime] = None,
) -> UsageObservation:
    """Keep the last real reading while its windows are open.

    Only a measured reading is held, and only over a read that is not
    measured. Windows that already reset are dropped from it, and once none
    is left open the new read wins whatever it is.
    """
    now = now or utc_now()
    if new.is_measured or previous is None or not previous.is_measured:
        return new

    open_windows = [
        window
        for window in previous.windows
        if window.window_end is not None and window.window_end > now
    ]
    if not open_windows:
        return new

    held = previous.model_copy(deep=True)
    held.windows = [
        window
        for window in held.windows
        if window.window_end is None or window.window_end > now
    ]
    reason = (new.error or "").strip()
    held.error = (
        "Showing the last real reading, because the latest read failed. " + reason
    ).strip()
    held.expected_refresh_seconds = new.expected_refresh_seconds
    held.notes[HELD_NOTE] = True
    held.notes["latest_source"] = new.source
    held.notes[CHECKED_NOTE] = now.isoformat()
    if new.plan and not held.plan:
        held.plan = new.plan
    return held


def observation_is_stale(
    observation: Optional[UsageObservation], now: Optional[datetime] = None
) -> bool:
    """True when this account should be read again.

    The interval belongs to the observation, because only the source that
    produced it knows how often it moves. A local file walk costs nothing and is
    recomputed every minute. A provider endpoint that counts requests against a
    rate limit is left alone for longer.
    """
    if observation is None:
        return True
    now = now or utc_now()
    # A held reading keeps its old measured time on purpose, so its age stays
    # honest. Retry on the time of the last attempt instead, or every page
    # poll would read the provider again.
    checked = observation.notes.get(CHECKED_NOTE) if observation.notes else None
    if checked:
        try:
            since = (now - datetime.fromisoformat(checked)).total_seconds()
        except (TypeError, ValueError):
            since = observation.age_seconds(now)
        return since >= observation.expected_refresh_seconds
    return observation.age_seconds(now) >= observation.expected_refresh_seconds
