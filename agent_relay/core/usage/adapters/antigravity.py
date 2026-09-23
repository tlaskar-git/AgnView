"""AntiGravity usage.

AntiGravity had no usage adapter at all. Provider dispatch was substring
matching over "claude", "chatgpt", "gemini", "deepseek" and "custom", with a
fallthrough to the local-harness probe, so the string "antigravity" matched
nothing and an AntiGravity account silently became an Ollama check against
localhost:11434. The Add Account dropdown had no AntiGravity option either, so
one could not be created in the first place. This module and the registry entry
beside it are what make it a real provider.

Two sources, in order.

The **desktop app's usage panel** carries the real figures. AntiGravity is an
Electron app whose packaged build still opens a Chromium debug port on loopback,
so its own window can be read while the app is open. That is handled in
``agent_relay.core.antigravity`` and adapted in ``agy_panel``. It needs the app
running with the model picker on screen, and both conditions are reported as
instructions rather than worked around.

The **CLI log** carries sign-in state and nothing else. Checked by inspection on
2026-09-22: ``~/.gemini/antigravity-cli/cli.log`` records
``GetG1Credits: starting fetch`` and ``doRefreshQuota: starting reload`` but
never the result; signing in leaves ``~/.gemini/oauth_creds.json`` untouched and
writes nothing readable to ``%APPDATA%/Antigravity/app_storage.json`` or the
Credential Manager; and the CLI's language server binds a fresh random port on
every run and exits with the process. So the log sits below the panel as the
explanation for an empty card, never as a source of numbers.

"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from ..base import AccountLike, SourceFn, UsageAdapter
from .agy_panel import fetch_panel
from .agy_cloud import fetch_antigravity
from ..models import (
    PlanInfo,
    UsageObservation,
)

CLI_LOG = Path.home() / ".gemini" / "antigravity-cli" / "cli.log"
DESKTOP_LOG = Path.home() / ".gemini" / "antigravity" / "cli.log"

REFRESH_SECONDS = 600

# glog, as the AntiGravity binary writes it:
#   E0922 18:07:00.658800  188 credits_manager.go:42] failed to refresh G1 ...
# The year is absent from the line, so it is taken from the file's own mtime.
_LOG_LINE = re.compile(
    r"^(?P<level>[IWEF])(?P<month>\d{2})(?P<day>\d{2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2}\.\d+)\s+\d+\s+"
    r"(?P<origin>[\w.]+\.go:\d+)\]\s+(?P<message>.*)$"
)

# Lines from the parts of AntiGravity that own quota and credits.
_RELEVANT_ORIGIN = re.compile(r"^(quota_manager|credits_manager)\.go:")

# Or a line from elsewhere that names one of those operations. The pattern is
# deliberately strict. A loose match on the bare word "quota" pulled in
# ``applyAuthResult: ... quotaProject=`` from the OAuth code, which is an
# authentication line and says nothing about how much quota is left.
_RELEVANT_MESSAGE = re.compile(
    r"\b(GetG1Credits|doRefreshQuota|G1 credits|quota (?:remaining|limit|usage|exhausted))\b",
    re.IGNORECASE,
)

# "You are not logged into Antigravity." and its neighbours.
_NOT_SIGNED_IN = re.compile(
    r"not logged in|not logged into|no token|token source", re.IGNORECASE
)

# A line announcing that work is about to begin is not an outcome. AntiGravity
# logs "GetG1Credits: starting fetch" immediately before the line that says
# whether it worked, and the announcement is often the newest of the two by a
# few milliseconds.
_ANNOUNCEMENT = re.compile(r"\b(starting|begin(ning)?|scheduling|queued)\b", re.IGNORECASE)

# Proof that a sign-in completed. AntiGravity logs these once the credential is
# in place, and they are what supersede an earlier "not logged in" failure.
#
# Without this check the adapter reported "not signed in" for a machine that had
# signed in seconds later: the failure was the newest *outcome*, and every line
# after it was an announcement, which the rule above correctly skips.
_LOGIN_SUCCEEDED = re.compile(
    r"refreshed after login|after login|applyAuthResult|Auth succeeded", re.IGNORECASE
)


def _log_paths() -> List[Path]:
    return [path for path in (CLI_LOG, DESKTOP_LOG) if path.exists()]


def _parse_log_line(line: str, year: int) -> Optional[Tuple[datetime, str, str, str]]:
    match = _LOG_LINE.match(line.strip())
    if not match:
        return None
    try:
        stamp = datetime.strptime(
            f"{year}{match.group('month')}{match.group('day')} {match.group('time')}",
            "%Y%m%d %H:%M:%S.%f",
        ).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return stamp, match.group("level"), match.group("origin"), match.group("message")


class _LogState:
    """What AntiGravity's log says about its own auth and quota work."""

    def __init__(self) -> None:
        self.newest_outcome: Optional[Tuple[datetime, str, str]] = None
        self.newest_announcement: Optional[Tuple[datetime, str, str]] = None
        self.newest_login: Optional[datetime] = None

    @property
    def any_activity(self) -> bool:
        return bool(self.newest_outcome or self.newest_announcement or self.newest_login)

    @property
    def signed_in(self) -> bool:
        """True when the last thing that happened was a successful sign-in.

        A sign-out failure only stands while nothing has signed in since.
        """
        if self.newest_login is None:
            return False
        if self.newest_outcome is None:
            return True
        stamp, _, message = self.newest_outcome
        if not _NOT_SIGNED_IN.search(message):
            return True
        return self.newest_login >= stamp


def _read_log_state() -> _LogState:
    """Scan the tail of each log for auth and quota events.

    Tail only, because these logs reach tens of megabytes and only the newest
    events matter.
    """
    state = _LogState()
    for path in _log_paths():
        try:
            year = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).year
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-4000:]
        except OSError:
            continue
        for line in lines:
            parsed = _parse_log_line(line, year)
            if parsed is None:
                continue
            stamp, level, origin, message = parsed

            if _LOGIN_SUCCEEDED.search(message):
                if state.newest_login is None or stamp > state.newest_login:
                    state.newest_login = stamp
                continue

            if not _RELEVANT_ORIGIN.match(origin) and not _RELEVANT_MESSAGE.search(message):
                continue

            candidate = (stamp, level, message)
            if _ANNOUNCEMENT.search(message):
                if state.newest_announcement is None or stamp > state.newest_announcement[0]:
                    state.newest_announcement = candidate
                continue
            if state.newest_outcome is None or stamp > state.newest_outcome[0]:
                state.newest_outcome = candidate
    return state


def fetch_log(account: AccountLike) -> Optional[UsageObservation]:
    """Report AntiGravity's sign-in state, and that it publishes no figure.

    This rung never invents a figure. Its job is to turn a blank card into the
    actual situation, which today is one of two things: AntiGravity is signed
    out and should be signed in, or it is signed in and reports nothing that a
    dashboard can read.
    """
    plan = PlanInfo(name="AntiGravity", label=None)
    state = _read_log_state()

    if not state.any_activity:
        if not _log_paths():
            return UsageObservation.unavailable(
                "agy_log",
                f"AntiGravity has written no log at {CLI_LOG}, so it has not run "
                "on this machine.",
                expected_refresh_seconds=REFRESH_SECONDS,
            )
        return UsageObservation.unavailable(
            "agy_log",
            "AntiGravity's log carries no sign-in or quota entry yet. Run agy "
            "once, then refresh.",
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )

    if not state.signed_in:
        stamp, _, message = state.newest_outcome or (None, None, "")
        cleaned = (message or "").strip().rstrip(".")
        observation = UsageObservation.unavailable(
            "agy_log",
            "AntiGravity is not signed in, so it reports no quota. Run agy and "
            "sign in, then refresh."
            + (f" AntiGravity's own log says: {cleaned}." if cleaned else ""),
            plan=plan,
            expected_refresh_seconds=REFRESH_SECONDS,
        )
        if stamp is not None:
            observation.measured_at = stamp
        return observation

    # Signed in. AntiGravity refreshes its credits and logs no figure with them,
    # keeps its credential in the running process, and binds a new random port
    # each run, so there is nothing left to read. Saying that is the honest
    # result, and it is different from being signed out.
    observation = UsageObservation.unavailable(
        "agy_log",
        # Short on purpose. The panel rung above owns the instruction, and the
        # ladder prints the highest rung's reason first, so repeating it here
        # gave the card the same sentence twice.
        "AntiGravity is signed in.",
        plan=plan,
        expected_refresh_seconds=REFRESH_SECONDS,
    )
    stamps = [state.newest_login]
    if state.newest_outcome:
        stamps.append(state.newest_outcome[0])
    if state.newest_announcement:
        stamps.append(state.newest_announcement[0])
    newest = max([s for s in stamps if s is not None], default=None)
    if newest is not None:
        observation.measured_at = newest
    observation.notes["signed_in"] = True
    return observation


def fetch_desktop_panel(account: AccountLike) -> Optional[UsageObservation]:
    """Read the figure from the AntiGravity desktop app's own window."""
    return fetch_panel("AntiGravity")


class AntiGravityAdapter(UsageAdapter):
    provider = "antigravity"
    display_name = "AntiGravity (AGY)"
    hint = "Reads AntiGravity's quota from Google with the app's own sign-in"

    def sources(self) -> List[Tuple[str, SourceFn]]:
        # Google's own quota answer comes first: it needs neither the app window
        # nor its model picker. The desktop app's panel is next, for a machine
        # where the sign-in cannot be read. The CLI log carries only sign-in
        # state, so it sits last as the explanation for an empty card.
        return [
            ("agy_cloud", fetch_antigravity),
            ("agy_panel", fetch_desktop_panel),
            ("agy_log", fetch_log),
        ]
