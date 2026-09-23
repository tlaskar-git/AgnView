"""Claude usage.

The top rung is Anthropic's own account usage endpoint, read with the bearer
token Claude Code already holds. That endpoint reports the real five-hour and
seven-day subscription windows with absolute reset instants, which is what the
Claude Code ``/usage`` command shows. Nothing else on the machine can produce a
share of a plan limit: the transcripts under ~/.claude/projects carry token
counts and no rate-limit data at all.

AgnView reads the token and never writes it. Rewriting the file Claude Code
owns would race Claude Code and risk the sign-in. The token lasts about eight
hours, and only a Claude Code session refreshes it, so on a machine where
Claude Code runs from the desktop app rather than the terminal the file went
stale overnight and the card fell back to a local token count. When the token
is close to expiry, AgnView now asks Claude Code itself to refresh it, by
running the smallest possible prompt with the claude command. Claude Code
stays the only writer of its own credential. Set AGNVIEW_CLAUDE_REFRESH=0 to
turn this off.
"""

import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import List, Optional, Tuple

import httpx

from ..base import AccountLike, SourceFn, UsageAdapter
from ..models import (
    CONFIDENCE_DERIVED,
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
    parse_instant,
    utc_now,
)
from ...claude_code_usage import (
    SESSION_WINDOW_HOURS,
    WEEK_WINDOW_DAYS,
    read_usage as read_claude_code_usage,
)

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The endpoint is served behind this beta gate. Without it the request is
# rejected even with a valid subscription token.
OAUTH_BETA = "oauth-2025-04-20"

CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
PROFILE_PATH = Path.home() / ".claude.json"

# The account endpoint recomputes on its own schedule and counts against a rate
# limit, so it is read at most this often.
OAUTH_REFRESH_SECONDS = 300
# A local file walk hits nothing remote, so it can be recomputed freely.
LOCAL_REFRESH_SECONDS = 60

# Ask Claude Code to refresh its sign-in this long before the token expires,
# so the card never sees an expired token while Claude Code is installed.
SIGN_IN_REFRESH_MARGIN_SECONDS = 30 * 60
# At most one attempt in this interval, so a broken Claude Code install is not
# started on every page poll.
SIGN_IN_REFRESH_RETRY_SECONDS = 15 * 60
SIGN_IN_REFRESH_TIMEOUT_SECONDS = 120
SIGN_IN_REFRESH_ENV = "AGNVIEW_CLAUDE_REFRESH"
# One word back from the smallest model. It is a real request, which is what
# makes Claude Code refresh the token, and it costs close to nothing.
SIGN_IN_REFRESH_PROMPT = "Reply with the single word OK."


# ---------------------------------------------------------------------------
# Token
# ---------------------------------------------------------------------------


def _read_token_from_file() -> Optional[dict]:
    if not CREDENTIALS_PATH.exists():
        return None
    try:
        data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def _read_token_from_keychain() -> Optional[dict]:
    """macOS keeps the credential in the Keychain rather than in the file."""
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def read_claude_oauth() -> Optional[dict]:
    """The Claude Code OAuth record, from wherever this platform keeps it."""
    if platform.system() == "Darwin":
        return _read_token_from_keychain() or _read_token_from_file()
    return _read_token_from_file()


def _plan_from_oauth(oauth: dict) -> PlanInfo:
    """Name the plan from the credential, which is Claude Code's own record."""
    subscription = (oauth.get("subscriptionType") or "").strip()
    tier = (oauth.get("rateLimitTier") or "").strip()
    name = f"Claude {subscription.title()}" if subscription else None
    label = None
    if tier:
        # "default_claude_max_20x" is the tier as Anthropic writes it. Keep the
        # digits as they are, so 20x does not become 20X.
        cleaned = tier.removeprefix("default_").replace("claude_", "")
        label = " ".join(
            word if any(ch.isdigit() for ch in word) else word.title()
            for word in cleaned.split("_")
        )
    return PlanInfo(name=name, label=label)


def _plan_from_profile() -> Optional[PlanInfo]:
    """Fall back to ~/.claude.json, which Claude Code also writes."""
    if not PROFILE_PATH.exists():
        return None
    try:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    oauth = data.get("oauthAccount") or {}
    org_type = (oauth.get("organizationType") or "").strip()
    tier = (oauth.get("organizationRateLimitTier") or "").strip()
    if not org_type and not tier:
        return None
    label = None
    if tier:
        cleaned = tier.removeprefix("default_").replace("claude_", "")
        label = " ".join(
            word if any(ch.isdigit() for ch in word) else word.title()
            for word in cleaned.split("_")
        )
    return PlanInfo(
        name=org_type.replace("_", " ").title() if org_type else None, label=label
    )


# ---------------------------------------------------------------------------
# Keeping the sign-in fresh
# ---------------------------------------------------------------------------

_refresh_lock = threading.Lock()
_last_refresh_attempt = 0.0
_last_refresh_error: Optional[str] = None


def _seconds_left(oauth: dict) -> Optional[float]:
    expires_at = oauth.get("expiresAt")
    if not isinstance(expires_at, (int, float)):
        return None
    return expires_at / 1000.0 - utc_now().timestamp()


def _claude_command() -> Optional[str]:
    return shutil.which("claude")


def refresh_sign_in_via_claude_code() -> Optional[str]:
    """Run Claude Code once so it refreshes its own token.

    Returns None on success, or the reason it did not happen. Attempts are
    rate limited across threads.
    """
    global _last_refresh_attempt, _last_refresh_error

    if os.environ.get(SIGN_IN_REFRESH_ENV, "1").strip().lower() in ("0", "false", "no", "off"):
        return f"Automatic sign-in refresh is off ({SIGN_IN_REFRESH_ENV}=0)."
    command = _claude_command()
    if not command:
        return "The claude command is not on PATH, so AgnView cannot ask Claude Code to refresh its sign-in."

    with _refresh_lock:
        now = time.monotonic()
        if _last_refresh_attempt and now - _last_refresh_attempt < SIGN_IN_REFRESH_RETRY_SECONDS:
            return _last_refresh_error
        _last_refresh_attempt = now
        flags = 0x08000000 if platform.system() == "Windows" else 0  # CREATE_NO_WINDOW
        try:
            with tempfile.TemporaryDirectory(prefix="agnview-claude-") as folder:
                result = subprocess.run(
                    [command, "-p", SIGN_IN_REFRESH_PROMPT, "--model", "haiku"],
                    cwd=folder,
                    capture_output=True,
                    text=True,
                    timeout=SIGN_IN_REFRESH_TIMEOUT_SECONDS,
                    creationflags=flags,
                )
        except subprocess.TimeoutExpired:
            _last_refresh_error = "Claude Code did not finish refreshing its sign-in in time."
            return _last_refresh_error
        except OSError as exc:
            _last_refresh_error = f"Could not start Claude Code to refresh its sign-in: {exc}"
            return _last_refresh_error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            _last_refresh_error = (
                "Claude Code could not refresh its sign-in"
                + (f": {detail[-1]}" if detail else ".")
            )
            return _last_refresh_error
        _last_refresh_error = None
        return None


def _refresh_in_background() -> None:
    threading.Thread(
        target=refresh_sign_in_via_claude_code, name="agnview-claude-sign-in", daemon=True
    ).start()


def fresh_claude_oauth() -> Tuple[Optional[dict], Optional[str]]:
    """The Claude Code OAuth record, refreshed by Claude Code when needed.

    An expired token is refreshed before the read, which blocks for the
    few seconds a Claude Code prompt takes. A token close to expiry is
    refreshed in the background while the current one is still used.
    """
    oauth = read_claude_oauth()
    if not oauth:
        return None, None
    left = _seconds_left(oauth)
    if left is None:
        return oauth, None
    if left <= 0:
        problem = refresh_sign_in_via_claude_code()
        return read_claude_oauth() or oauth, problem
    if left <= SIGN_IN_REFRESH_MARGIN_SECONDS:
        _refresh_in_background()
    return oauth, None


# ---------------------------------------------------------------------------
# Rung 1: the account usage endpoint
# ---------------------------------------------------------------------------

# The response carries a normalised ``limits`` array plus a set of codenamed
# keys for unreleased or inapplicable limits, almost all null. Only the array is
# read. These are the kinds it uses.
_KIND_LABELS = {
    "session": f"Session, last {SESSION_WINDOW_HOURS} hours",
    "weekly_all": f"Weekly, all models, {WEEK_WINDOW_DAYS} days",
    "weekly_scoped": "Weekly, scoped",
    "weekly_opus": "Weekly, Opus",
    "weekly_sonnet": "Weekly, Sonnet",
    "weekly_oauth_apps": "Weekly, connected apps",
}


def _scope_label(entry: dict) -> Optional[str]:
    """The model or surface a scoped limit applies to, when there is one."""
    scope = entry.get("scope")
    if not isinstance(scope, dict):
        return None
    model = scope.get("model")
    if isinstance(model, dict):
        name = (model.get("display_name") or "").strip()
        if name:
            return name
    surface = scope.get("surface")
    if isinstance(surface, dict):
        name = (surface.get("display_name") or "").strip()
        if name:
            return name
    if isinstance(surface, str) and surface.strip():
        return surface.strip()
    return None


def _window_from_limit(entry: dict, window_start) -> Optional[UsageWindow]:
    percent = entry.get("percent")
    if percent is None:
        return None
    try:
        used = float(percent)
    except (TypeError, ValueError):
        return None

    kind = (entry.get("kind") or "").strip()
    group = (entry.get("group") or "").strip()
    label = _KIND_LABELS.get(kind) or kind.replace("_", " ").title() or "Limit"
    scoped = _scope_label(entry)
    if scoped:
        label = f"{label} ({scoped})" if kind != "weekly_scoped" else f"Weekly, {scoped}"

    return UsageWindow(
        key=WINDOW_SESSION if group == "session" else WINDOW_WEEK,
        label=label,
        unit=UNIT_PERCENT,
        used=used,
        limit=100.0,
        window_start=window_start if group == "weekly" else None,
        window_end=parse_instant(entry.get("resets_at")),
        severity=(entry.get("severity") or None),
        is_active=bool(entry.get("is_active")),
    )


def fetch_oauth_api(account: AccountLike) -> Optional[UsageObservation]:
    """Read the real subscription windows from Anthropic's account usage API."""
    cred = (account.credential or "").strip()
    if cred.startswith("sk-ant-"):
        # This account is an API key, not a subscription. Let the next rung
        # handle it rather than reading a different account's windows.
        return None

    oauth, refresh_problem = fresh_claude_oauth()
    if not oauth:
        return UsageObservation.unavailable(
            "claude_oauth_api",
            "No Claude Code sign-in was found on this machine. Sign in to Claude "
            "Code, or add an Anthropic API key to this account.",
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )

    token = (oauth.get("accessToken") or "").strip()
    if not token:
        return UsageObservation.unavailable(
            "claude_oauth_api",
            "The Claude Code credential carries no access token.",
            plan=_plan_from_oauth(oauth),
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )

    plan = _plan_from_oauth(oauth)
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)):
        if expires_at / 1000.0 <= utc_now().timestamp():
            return UsageObservation.unavailable(
                "claude_oauth_api",
                "The Claude Code sign-in has expired. "
                + (refresh_problem or "Claude Code did not renew it.")
                + " Run any claude command, or sign in again with claude auth login.",
                plan=plan,
                expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
            )

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": OAUTH_BETA,
        "Accept": "application/json",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            res = client.get(USAGE_URL, headers=headers)
    except Exception as exc:
        return UsageObservation.unavailable(
            "claude_oauth_api",
            f"Could not reach the Anthropic account usage API: {exc}",
            plan=plan,
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )

    if res.status_code in (401, 403):
        return UsageObservation.unavailable(
            "claude_oauth_api",
            "Anthropic refused the Claude Code sign-in on this machine. Start "
            "Claude Code to refresh it.",
            plan=plan,
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )
    if res.status_code == 429:
        return UsageObservation.unavailable(
            "claude_oauth_api",
            "The Anthropic account usage API is rate limiting this machine. The "
            "next scheduled read will try again.",
            plan=plan,
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )
    if res.status_code != 200:
        return UsageObservation.unavailable(
            "claude_oauth_api",
            f"The Anthropic account usage API returned status {res.status_code}.",
            plan=plan,
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )

    try:
        data = res.json()
    except ValueError:
        return UsageObservation.unavailable(
            "claude_oauth_api",
            "The Anthropic account usage API returned no usage data.",
            plan=plan,
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )

    breakdown_block = data.get("seven_day_breakdown") or {}
    week_start = parse_instant(breakdown_block.get("window_started_at"))

    limits = data.get("limits")
    windows: List[UsageWindow] = []
    if isinstance(limits, list):
        for entry in limits:
            if isinstance(entry, dict):
                window = _window_from_limit(entry, week_start)
                if window is not None:
                    windows.append(window)

    if not windows:
        return UsageObservation.unavailable(
            "claude_oauth_api",
            "Anthropic reported no limit windows for this account.",
            plan=plan,
            expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
        )

    # Attach the per-surface split to the unscoped weekly window, which is what
    # it describes. Anthropic reports these as shares of the weekly total.
    rows = breakdown_block.get("rows")
    if isinstance(rows, list):
        children: List[UsageWindow] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            percent = row.get("percent")
            name = (row.get("display_name") or row.get("key") or "").strip()
            if percent is None or not name:
                continue
            try:
                value = float(percent)
            except (TypeError, ValueError):
                continue
            children.append(
                UsageWindow(
                    key=f"week_surface_{row.get('key') or name}",
                    label=name,
                    unit=UNIT_PERCENT,
                    used=value,
                    limit=100.0,
                    window_start=week_start,
                )
            )
        if children:
            for window in windows:
                if window.key == WINDOW_WEEK and not window.is_active:
                    window.breakdown = children
                    break
            else:
                for window in windows:
                    if window.key == WINDOW_WEEK:
                        window.breakdown = children
                        break

    observation = UsageObservation(
        source="claude_oauth_api",
        confidence=CONFIDENCE_MEASURED,
        windows=windows,
        plan=plan,
        expected_refresh_seconds=OAUTH_REFRESH_SECONDS,
    )

    # Paid overage, when the account has it switched on. Reported separately
    # because it is money, not a share of a plan window.
    extra = data.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled"):
        utilisation = extra.get("utilization")
        if utilisation is not None:
            try:
                observation.windows.append(
                    UsageWindow(
                        key="credits",
                        label="Extra usage credits",
                        unit=UNIT_PERCENT,
                        used=float(utilisation),
                        limit=100.0,
                    )
                )
            except (TypeError, ValueError):
                pass

    as_of = parse_instant(breakdown_block.get("as_of"))
    if as_of is not None:
        observation.measured_at = as_of
    return observation


# ---------------------------------------------------------------------------
# Rung 2: an Anthropic API key
# ---------------------------------------------------------------------------


def _header_int(headers, name: str) -> Optional[int]:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def fetch_api_key(account: AccountLike) -> Optional[UsageObservation]:
    """Read the per-minute rate limit an Anthropic API key is subject to.

    This is a rate limit, not a subscription window, and it is labelled as one.
    """
    cred = (account.credential or "").strip()
    if not cred.startswith("sk-ant-"):
        return None

    headers = {
        "x-api-key": cred,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get("https://api.anthropic.com/v1/models", headers=headers)
    except Exception as exc:
        return UsageObservation.unavailable(
            "claude_api_key", f"Could not reach the Anthropic API: {exc}"
        )

    if res.status_code == 401:
        return UsageObservation.unavailable(
            "claude_api_key", "Invalid Anthropic API key."
        )
    if res.status_code not in (200, 400):
        return UsageObservation.unavailable(
            "claude_api_key", f"The Anthropic API returned status {res.status_code}."
        )

    plan = PlanInfo(name="Anthropic API", label="API key")
    tok_limit = _header_int(res.headers, "anthropic-ratelimit-tokens-limit")
    tok_rem = _header_int(res.headers, "anthropic-ratelimit-tokens-remaining")
    req_limit = _header_int(res.headers, "anthropic-ratelimit-requests-limit")
    req_rem = _header_int(res.headers, "anthropic-ratelimit-requests-remaining")
    reset = parse_instant(
        res.headers.get("anthropic-ratelimit-tokens-reset")
        or res.headers.get("anthropic-ratelimit-requests-reset")
    )

    windows: List[UsageWindow] = []
    if tok_limit is not None and tok_rem is not None:
        windows.append(
            UsageWindow(
                key=WINDOW_RATE_LIMIT,
                label="API tokens per minute",
                unit=UNIT_TOKENS,
                used=float(max(0, tok_limit - tok_rem)),
                limit=float(tok_limit),
                window_end=reset,
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
                window_end=reset,
            )
        )

    if not windows:
        return UsageObservation.unavailable(
            "claude_api_key",
            "The Anthropic API key is valid but returned no rate-limit headers, "
            "so there is no usage figure to show.",
            plan=plan,
        )

    return UsageObservation(
        source="claude_api_key",
        confidence=CONFIDENCE_MEASURED,
        windows=windows,
        plan=plan,
        expected_refresh_seconds=60,
    )


# ---------------------------------------------------------------------------
# Rung 3: local transcripts
# ---------------------------------------------------------------------------


def _scope_sub_line(turns: int, projects: int) -> str:
    turn_word = "turn" if turns == 1 else "turns"
    if projects <= 0:
        return f"{turns} {turn_word}"
    project_word = "project" if projects == 1 else "projects"
    return f"{turns} {turn_word} across {projects} {project_word}, subagents included"


def fetch_transcripts(account: AccountLike) -> Optional[UsageObservation]:
    """Count tokens from the transcripts Claude Code wrote on this machine.

    This is the last rung and it reports no percentage, because none can be
    derived: the transcripts carry token counts and no rate-limit data. The
    figure is a count, marked derived, with no denominator and therefore no bar.
    """
    usage = read_claude_code_usage()
    plan = _plan_from_profile()
    if usage is None:
        return UsageObservation.unavailable(
            "claude_transcripts",
            "Claude Code has written no transcripts under "
            f"{Path.home() / '.claude' / 'projects'}, so there is nothing to count.",
            plan=plan,
            expected_refresh_seconds=LOCAL_REFRESH_SECONDS,
        )

    # The sub-line carries only what the token total does not already say:
    # how many turns produced it and how widely they were spread. Repeating the
    # token figure here rendered it twice, back to back, on the card.
    windows = [
        UsageWindow(
            key=WINDOW_SESSION,
            label=f"Counted, last {SESSION_WINDOW_HOURS} hours on this machine",
            sub_label=_scope_sub_line(usage.session_turns, usage.projects_counted),
            unit=UNIT_TOKENS,
            used=float(usage.session_tokens),
            limit=None,
            severity=None,
        ),
        UsageWindow(
            key=WINDOW_WEEK,
            label=f"Counted, last {WEEK_WINDOW_DAYS} days on this machine",
            sub_label=_scope_sub_line(usage.week_turns, usage.projects_counted),
            unit=UNIT_TOKENS,
            used=float(usage.week_tokens),
            limit=None,
            severity=None,
        ),
    ]
    return UsageObservation(
        source="claude_transcripts",
        confidence=CONFIDENCE_DERIVED,
        windows=windows,
        plan=plan,
        expected_refresh_seconds=LOCAL_REFRESH_SECONDS,
        notes={
            "session_scope": _scope_sub_line(usage.session_turns, usage.projects_counted),
            "week_scope": _scope_sub_line(usage.week_turns, usage.projects_counted),
            "transcripts_read": usage.transcripts_read,
        },
    )


class ClaudeAdapter(UsageAdapter):
    provider = "claude"
    display_name = "Claude (Anthropic)"
    hint = "Claude Code sign-in, or an sk-ant- API key"

    def sources(self) -> List[Tuple[str, SourceFn]]:
        return [
            ("claude_oauth_api", fetch_oauth_api),
            ("claude_api_key", fetch_api_key),
            ("claude_transcripts", fetch_transcripts),
        ]
