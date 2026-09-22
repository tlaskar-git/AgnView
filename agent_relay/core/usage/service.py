"""The entry point the API calls.

One function fetches an observation for an account, and one decides whether a
stored observation is old enough to need refreshing. Nothing here merges an old
observation into a new one.

That last point is the fix for the worst defect in the previous design. It kept
a manually synced percentage for seven days and wrote it back over every fresh
read, re-marking the account active and clearing its error, so a figure measured
three days earlier was presented as current with its age shown nowhere. A read
now replaces the observation outright. An older figure can only win by being
stored as its own observation and being genuinely newer, which it cannot be.
"""

from datetime import datetime
from typing import Optional

from .base import AccountLike
from .models import UsageObservation, utc_now
from .registry import get_adapter


def fetch_observation(account: AccountLike) -> UsageObservation:
    """Read this account from the best source its adapter can reach.

    Raises ``UnknownProvider`` when the account names a provider with no
    adapter. The caller turns that into a clear error rather than probing
    something unrelated.
    """
    adapter = get_adapter(account.provider)
    return adapter.fetch(account)


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
    return observation.age_seconds(now) >= observation.expected_refresh_seconds
