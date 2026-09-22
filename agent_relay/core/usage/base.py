"""The adapter contract.

An adapter owns one provider. It declares an ordered ladder of sources, tries
each in turn, and returns the first observation that carries a measurement. The
observation records which source produced it, so the card can name it and
degradation is never silent.

The previous design dispatched on substring matching with a fallthrough to the
local-harness probe, so a provider nobody had written an adapter for became an
Ollama check against localhost:11434 without saying so. AntiGravity was exactly
that case. A ladder makes the fallback explicit and visible instead.
"""

from typing import Callable, List, Optional, Protocol, Tuple

from .models import (
    CONFIDENCE_UNAVAILABLE,
    UsageObservation,
)


class AccountLike(Protocol):
    """What an adapter needs from an account. Kept narrow on purpose."""

    id: str
    provider: str
    name: str
    credential: str
    base_url: Optional[str]
    plan_name: str


# A source is a name plus a callable that returns an observation, or None when
# that source does not apply to this account at all (no credential of its kind,
# no file on disk). Returning None is different from returning an unavailable
# observation: None means "not my case, try the next rung", while unavailable
# means "my case, and here is why it failed".
SourceFn = Callable[[AccountLike], Optional[UsageObservation]]


class UsageAdapter:
    """Base adapter. Subclasses declare ``provider`` and ``sources``."""

    provider: str = ""
    # Shown in the Add Account dropdown, which is generated from the registry
    # so a registered adapter cannot be missing from the UI.
    display_name: str = ""
    # Help text for the dropdown option.
    hint: str = ""

    def sources(self) -> List[Tuple[str, SourceFn]]:
        """The ladder, highest quality first."""
        raise NotImplementedError

    def fetch(self, account: AccountLike) -> UsageObservation:
        """Walk the ladder and return the first real result.

        A source that raises is recorded as a failure on that rung and the walk
        continues. One unreachable endpoint must not blank a card that a lower
        rung can still fill.
        """
        failures: List[str] = []
        last_unavailable: Optional[UsageObservation] = None

        for source_name, source_fn in self.sources():
            try:
                observation = source_fn(account)
            except Exception as exc:  # one bad rung must not end the walk
                failures.append(f"{source_name}: {exc}")
                continue

            if observation is None:
                # This source does not apply to this account.
                continue

            if observation.confidence != CONFIDENCE_UNAVAILABLE:
                if failures:
                    observation.notes["skipped_sources"] = failures
                return observation

            # Applicable but empty. Remember the reason and keep walking, so a
            # lower rung with a real figure still wins.
            last_unavailable = observation
            if observation.error:
                failures.append(f"{source_name}: {observation.error}")

        if last_unavailable is not None:
            if failures:
                last_unavailable.notes["skipped_sources"] = failures
            return last_unavailable

        reason = (
            "No usage source applies to this account. "
            + "; ".join(failures)
            if failures
            else "No usage source applies to this account."
        )
        return UsageObservation.unavailable("none", reason)
