"""Live usage for Claude Code, Codex, Gemini, DeepSeek and custom harnesses.

Every number here comes from a real source: a file the tool itself wrote, or a
response from the provider. When there is no source to read, the account is
marked unavailable with the reason, and the dashboard says so. Nothing in this
module invents a figure, and nothing carries a placeholder forward as if it had
been measured.
"""

import json
from pathlib import Path
import httpx
from datetime import datetime, timezone

from .claude_code_usage import (
    SESSION_WINDOW_HOURS,
    WEEK_WINDOW_DAYS,
    read_usage as read_claude_code_usage,
)
from .models import UsageAccount


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mark_unavailable(account: UsageAccount, reason: str) -> UsageAccount:
    """Say plainly that nothing could be measured, rather than showing a zero."""
    account.status = "unavailable"
    account.error_message = reason
    account.session_percent_used = None
    account.session_percent_left = None
    account.weekly_percent_used = None
    account.weekly_percent_left = None
    account.percent_used = None
    account.tokens_used = None
    account.tokens_limit = None
    account.tokens_remaining = None
    account.requests_used = None
    account.requests_limit = None
    account.requests_remaining = None
    # A per-model breakdown left over from an earlier, working check (or from
    # the old fabricated defaults) must not survive into an "unavailable"
    # result, or the summary says unavailable while the detail table below it
    # still shows invented per-model percentages.
    account.weekly_breakdown = None
    account.last_checked = _get_utc_now_iso()
    return account


def fetch_account_usage(account: UsageAccount) -> UsageAccount:
    """Dispatches live usage query based on provider."""
    provider = account.provider.lower()
    try:
        if "claude" in provider or "anthropic" in provider:
            return _fetch_claude_live(account)
        elif "chatgpt" in provider or "openai" in provider:
            return _fetch_chatgpt_live(account)
        elif "gemini" in provider or "google" in provider:
            return _fetch_gemini_live(account)
        elif "deepseek" in provider:
            return _fetch_deepseek_live(account)
        elif "custom" in provider or "ollama" in provider or "local" in provider:
            return _fetch_custom_live(account)
        else:
            return _fetch_custom_live(account)
    except Exception as e:
        account.status = "error"
        account.error_message = f"Fetch failed: {str(e)}"
        account.last_checked = _get_utc_now_iso()
        return account


def _scope_sub_line(turns: int, projects: int) -> str:
    """Say how a Claude Code token total was reached, without restating it.

    The count covers every project Claude Code has run on this machine, and
    the turns subagents took inside them, because the account is billed for
    all of it. Naming the project count keeps a large turn count explainable.
    """
    turn_word = "turn" if turns == 1 else "turns"
    if projects <= 0:
        return f"{turns} {turn_word}"
    project_word = "project" if projects == 1 else "projects"
    return f"{turns} {turn_word} across {projects} {project_word}, subagents included"


def _read_claude_plan(account: UsageAccount) -> bool:
    """Name the plan from the Claude Code profile on disk. True when it is read.

    ~/.claude.json is written by Claude Code itself, so organizationType and
    organizationRateLimitTier are the real plan of the signed-in account. It
    carries no quota figures, which is why nothing else is taken from it.
    """
    profile = Path.home() / ".claude.json"
    if not profile.exists():
        return False
    try:
        data = json.loads(profile.read_text(encoding="utf-8"))
    except Exception:
        return False

    oauth = data.get("oauthAccount") or {}
    org_type = (oauth.get("organizationType") or "").strip()
    rate_tier = (oauth.get("organizationRateLimitTier") or "").strip()
    if not org_type and not rate_tier:
        return False

    if org_type:
        account.plan_name = org_type.replace("_", " ").title()
    if rate_tier:
        # "default_claude_max_20x" is the tier as Anthropic writes it.
        label = rate_tier.removeprefix("default_").replace("claude_", "")
        # Title case each word except the ones carrying a number, so a tier
        # like 20x stays 20x rather than becoming 20X.
        account.plan_label = " ".join(
            word if any(ch.isdigit() for ch in word) else word.title()
            for word in label.split("_")
        )
    return True


def _fetch_claude_live(account: UsageAccount) -> UsageAccount:
    """Report Claude Code usage measured from the transcripts on this machine.

    Anthropic publishes no local file and no endpoint that says how much of a
    subscription window is spent, so no percentage is reported for a Claude
    Code account. What can be counted exactly is the tokens Claude Code has
    recorded, and that is what the dashboard shows.
    """
    cred = (account.credential or "").strip()
    now = _get_utc_now_iso()

    if cred.startswith("sk-ant-"):
        return _fetch_anthropic_api_key(account, cred)

    plan_read = _read_claude_plan(account)
    usage = read_claude_code_usage()
    if usage is None:
        if not plan_read:
            return _mark_unavailable(
                account,
                "No Claude Code data on this machine. Sign in to Claude Code, or "
                "add an Anthropic API key to this account.",
            )
        return _mark_unavailable(
            account,
            f"Claude Code is signed in but has written no transcripts under "
            f"{Path.home() / '.claude' / 'projects'}, so there is nothing to count yet.",
        )

    # The card prints the token total itself, from session_tokens_used. The
    # sub-line therefore carries only what the total does not already say:
    # how many turns produced it and how widely they were spread. Repeating
    # the token figure here rendered it twice, back to back, on the card.
    account.session_title = f"Last {SESSION_WINDOW_HOURS} hours, this machine"
    account.session_reset_time = _scope_sub_line(usage.session_turns, usage.projects_counted)
    account.session_tokens_used = usage.session_tokens
    account.weekly_title = f"Last {WEEK_WINDOW_DAYS} days, this machine"
    account.weekly_reset_time = _scope_sub_line(usage.week_turns, usage.projects_counted)
    account.weekly_tokens_used = usage.week_tokens

    # Counted, not estimated. The share of the plan limit stays unknown, so
    # every percentage field stays empty and the dashboard renders no bar.
    account.tokens_used = usage.session_tokens
    account.tokens_limit = None
    account.tokens_remaining = None
    account.session_percent_used = None
    account.session_percent_left = None
    account.weekly_percent_used = None
    account.weekly_percent_left = None
    account.percent_used = None
    account.weekly_breakdown = None
    account.reset_time = account.session_reset_time
    account.status = "active"
    account.error_message = None
    account.last_checked = now
    return account


def _fetch_anthropic_api_key(account: UsageAccount, cred: str) -> UsageAccount:
    """Read the per-minute rate limit an Anthropic API key is subject to.

    This is a rate limit, not a subscription window, and it is labelled as one.
    """
    headers = {
        "x-api-key": cred,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.get("https://api.anthropic.com/v1/models", headers=headers)
    except Exception as e:
        return _mark_unavailable(account, f"Could not reach the Anthropic API: {e}")

    if res.status_code == 401:
        account.status = "error"
        account.error_message = "Invalid Anthropic API key."
        account.last_checked = _get_utc_now_iso()
        return account
    if res.status_code not in (200, 400):
        return _mark_unavailable(
            account, f"The Anthropic API returned status {res.status_code}."
        )

    headers_back = res.headers
    req_limit = _header_int(headers_back, "anthropic-ratelimit-requests-limit")
    req_rem = _header_int(headers_back, "anthropic-ratelimit-requests-remaining")
    tok_limit = _header_int(headers_back, "anthropic-ratelimit-tokens-limit")
    tok_rem = _header_int(headers_back, "anthropic-ratelimit-tokens-remaining")
    reset_val = headers_back.get("anthropic-ratelimit-tokens-reset") or headers_back.get(
        "anthropic-ratelimit-requests-reset"
    )

    if tok_limit is None and req_limit is None:
        return _mark_unavailable(
            account,
            "The Anthropic API key is valid but returned no rate-limit headers, "
            "so there is no usage figure to show.",
        )

    account.plan_name = account.plan_name or "Anthropic API"
    account.plan_label = "API key"
    account.session_title = "API rate limit"
    account.requests_limit = req_limit
    account.requests_remaining = req_rem
    account.requests_used = (
        max(0, req_limit - req_rem) if req_limit is not None and req_rem is not None else None
    )
    account.tokens_limit = tok_limit
    account.tokens_remaining = tok_rem
    account.tokens_used = (
        max(0, tok_limit - tok_rem) if tok_limit is not None and tok_rem is not None else None
    )

    if tok_limit and account.tokens_used is not None:
        account.session_percent_used = round(account.tokens_used / tok_limit * 100.0, 1)
        account.session_percent_left = round(100.0 - account.session_percent_used, 1)
        account.percent_used = account.session_percent_used
    account.session_reset_time = f"Resets at {reset_val}" if reset_val else None
    account.reset_time = account.session_reset_time
    account.weekly_title = None
    account.weekly_reset_time = None
    account.weekly_percent_used = None
    account.weekly_percent_left = None
    account.status = "active"
    account.error_message = None
    account.last_checked = _get_utc_now_iso()
    return account


def _header_int(headers, name: str):
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    """Return a float only when the provider actually sent a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _countdown(seconds, as_days: bool):
    """Render a reset countdown, or nothing when the provider did not send one."""
    if seconds is None:
        return None
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return None
    if total < 0:
        return None
    if as_days:
        return f"Resets in {total // 86400}d {(total % 86400) // 3600}h"
    return f"Resets in {total // 3600}h {(total % 3600) // 60}m"


def _fetch_chatgpt_live(account: UsageAccount) -> UsageAccount:
    """Report the Codex usage windows ChatGPT publishes for this sign-in."""
    cred = (account.credential or "").strip()
    now = _get_utc_now_iso()

    account.session_title = "5-hour limit"
    account.weekly_title = "Weekly limit"

    # 1. Probe live ChatGPT wham usage endpoint via local Codex auth token or credential JWT
    codex_auth = Path.home() / ".codex" / "auth.json"
    access_tok = None
    account_id = None
    if codex_auth.exists():
        try:
            cj = json.loads(codex_auth.read_text(encoding="utf-8"))
            toks = cj.get("tokens") or {}
            access_tok = toks.get("access_token")
            account_id = toks.get("account_id")
        except Exception:
            pass

    token_to_use = cred if (cred.startswith("eyJ") and len(cred) > 50) else access_tok
    if token_to_use:
        wham_headers = {
            "Authorization": f"Bearer {token_to_use}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json"
        }
        if account_id:
            wham_headers["ChatGPT-Account-ID"] = account_id

        try:
            with httpx.Client(timeout=8.0) as client:
                res = client.get("https://chatgpt.com/backend-api/wham/usage", headers=wham_headers)
        except Exception as e:
            return _mark_unavailable(account, f"Could not reach the ChatGPT usage API: {e}")

        if res.status_code in (401, 403):
            return _mark_unavailable(
                account,
                "ChatGPT refused the Codex sign-in on this machine. Sign in to "
                "Codex again to refresh it.",
            )
        if res.status_code != 200:
            return _mark_unavailable(
                account, f"The ChatGPT usage API returned status {res.status_code}."
            )

        try:
            data = res.json()
        except ValueError:
            return _mark_unavailable(account, "The ChatGPT usage API returned no usage data.")

        plan_type = (data.get("plan_type") or "").strip()
        if plan_type:
            account.plan_name = f"ChatGPT {plan_type.title()}"
            account.plan_label = plan_type.title()

        rl = data.get("rate_limit") or {}
        pw = rl.get("primary_window") or {}
        sw = rl.get("secondary_window") or {}
        if not pw and not sw:
            return _mark_unavailable(
                account, "ChatGPT reported no rate-limit windows for this account."
            )

        # Primary window: the 5-hour limit, as ChatGPT reports it.
        p_used = _as_float(pw.get("used_percent"))
        if p_used is not None:
            account.session_percent_used = round(p_used, 1)
            account.session_percent_left = round(100.0 - p_used, 1)
        account.session_reset_time = _countdown(pw.get("reset_after_seconds"), as_days=False)

        # Secondary window: the weekly limit.
        s_used = _as_float(sw.get("used_percent"))
        if s_used is not None:
            account.weekly_percent_used = round(s_used, 1)
            account.weekly_percent_left = round(100.0 - s_used, 1)
        account.weekly_reset_time = _countdown(sw.get("reset_after_seconds"), as_days=True)

        account.percent_used = account.session_percent_used
        account.reset_time = account.session_reset_time
        # ChatGPT reports a share of each window and no token count, so the
        # token fields stay empty rather than being back-derived from a percent.
        account.tokens_used = None
        account.tokens_limit = None
        account.tokens_remaining = None
        account.status = "active"
        account.error_message = None
        account.last_checked = now
        return account

    # 2. An OpenAI API key reports a per-minute rate limit, not a plan window.
    if cred.startswith("sk-") and not cred.startswith("sk-ant-"):
        headers = {"Authorization": f"Bearer {cred}"}
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get("https://api.openai.com/v1/models", headers=headers)
        except Exception as e:
            return _mark_unavailable(account, f"Could not reach the OpenAI API: {e}")

        if res.status_code == 401:
            account.status = "error"
            account.error_message = "Invalid OpenAI API key."
            account.last_checked = now
            return account
        if res.status_code != 200:
            return _mark_unavailable(
                account, f"The OpenAI API returned status {res.status_code}."
            )

        h = res.headers
        req_limit = _header_int(h, "x-ratelimit-limit-requests")
        req_rem = _header_int(h, "x-ratelimit-remaining-requests")
        tok_limit = _header_int(h, "x-ratelimit-limit-tokens")
        tok_rem = _header_int(h, "x-ratelimit-remaining-tokens")
        if tok_limit is None and req_limit is None:
            return _mark_unavailable(
                account,
                "The OpenAI API key is valid but returned no rate-limit headers, "
                "so there is no usage figure to show.",
            )

        account.session_title = "API rate limit"
        account.weekly_title = None
        account.weekly_reset_time = None
        account.requests_limit = req_limit
        account.requests_remaining = req_rem
        account.requests_used = (
            max(0, req_limit - req_rem) if req_limit is not None and req_rem is not None else None
        )
        account.tokens_limit = tok_limit
        account.tokens_remaining = tok_rem
        account.tokens_used = (
            max(0, tok_limit - tok_rem) if tok_limit is not None and tok_rem is not None else None
        )
        if tok_limit and account.tokens_used is not None:
            account.session_percent_used = round(account.tokens_used / tok_limit * 100.0, 1)
            account.session_percent_left = round(100.0 - account.session_percent_used, 1)
        reset_tok = h.get("x-ratelimit-reset-tokens")
        account.session_reset_time = f"Resets in {reset_tok}" if reset_tok else None
        account.percent_used = account.session_percent_used
        account.reset_time = account.session_reset_time
        account.status = "active"
        account.error_message = None
        account.last_checked = now
        return account

    return _mark_unavailable(
        account,
        "No Codex sign-in was found at ~/.codex/auth.json and this account "
        "carries no OpenAI API key, so there is nothing to read usage from.",
    )


def _fetch_gemini_live(account: UsageAccount) -> UsageAccount:
    """Report Gemini and AntiGravity usage, when there is any to report.

    Neither Gemini nor AntiGravity writes a quota file on this machine, and
    neither publishes an endpoint that says how much of a window is spent. An
    AI Studio API key can be checked for validity and nothing more. So the only
    honest answer for a signed-in Gemini or AntiGravity account is that usage is
    unavailable, with the reason attached.
    """
    cred = (account.credential or "").strip()

    if cred.startswith("AIza"):
        try:
            with httpx.Client(timeout=10.0) as client:
                res = client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    params={"key": cred},
                )
        except Exception as e:
            return _mark_unavailable(account, f"Could not reach the Gemini API: {e}")

        if res.status_code in (400, 403):
            account.status = "error"
            account.error_message = "Invalid Google Gemini API key."
            account.last_checked = _get_utc_now_iso()
            return account
        if res.status_code != 200:
            return _mark_unavailable(
                account, f"The Gemini API returned status {res.status_code}."
            )

        account.plan_name = "Gemini AI Studio"
        account.plan_label = "API key"
        # The models endpoint confirms the key works. Google publishes no quota
        # figure with it, so there is no usage number to show.
        return _mark_unavailable(
            account,
            "The Gemini API key is valid. Google publishes no quota figure for "
            "it, so there is no usage to show.",
        )

    account_file = Path.home() / ".gemini" / "google_accounts.json"
    if account_file.exists():
        try:
            active = json.loads(account_file.read_text(encoding="utf-8")).get("active")
        except Exception:
            active = None
        if active:
            account.plan_name = "Gemini"
            return _mark_unavailable(
                account,
                f"Signed in as {active}. Neither Gemini nor AntiGravity reports "
                "usage on this machine, so there is no figure to show.",
            )

    return _mark_unavailable(
        account,
        "No Gemini or AntiGravity sign-in was found on this machine, and neither "
        "tool reports usage locally.",
    )


def _fetch_deepseek_live(account: UsageAccount) -> UsageAccount:
    """Report the DeepSeek balance the account API returns, and nothing else."""
    cred = (account.credential or "").strip()
    now = _get_utc_now_iso()
    base_url = (account.base_url or "https://api.deepseek.com").rstrip("/")

    account.plan_name = account.plan_name or "DeepSeek"
    account.plan_label = "Pay as you go"
    account.session_title = "Balance"
    account.weekly_title = None
    account.weekly_reset_time = None

    if not cred:
        return _mark_unavailable(account, "This DeepSeek account carries no API key.")

    headers = {"Authorization": f"Bearer {cred}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=10.0) as client:
            res_models = client.get(f"{base_url}/models", headers=headers)
            if res_models.status_code in (401, 403):
                account.status = "error"
                account.error_message = "Invalid DeepSeek API key."
                account.last_checked = now
                return account
            if res_models.status_code != 200:
                return _mark_unavailable(
                    account, f"DeepSeek returned status {res_models.status_code}."
                )

            balance = None
            if "api.deepseek.com" in base_url:
                try:
                    res_bal = client.get(f"{base_url}/user/balance", headers=headers)
                    if res_bal.status_code == 200:
                        infos = (res_bal.json() or {}).get("balance_infos") or []
                        if infos:
                            balance = infos[0]
                except Exception:
                    balance = None
    except Exception as e:
        return _mark_unavailable(account, f"Could not reach DeepSeek: {e}")

    if balance is None:
        return _mark_unavailable(
            account,
            "The DeepSeek key is valid but the balance endpoint returned nothing, "
            "so there is no usage figure to show.",
        )

    currency = balance.get("currency", "USD")
    total = _as_float(balance.get("total_balance"))
    topped_up = _as_float(balance.get("topped_up_balance"))
    account.cost_used_usd = round(topped_up, 2) if topped_up is not None else None
    account.session_reset_time = (
        f"Balance: {currency} {total:.2f}" if total is not None else "Balance not reported"
    )
    account.reset_time = account.session_reset_time
    # DeepSeek reports money, not a share of a window, so no percentage is set.
    account.session_percent_used = None
    account.session_percent_left = None
    account.percent_used = None
    account.status = "active"
    account.error_message = None
    account.last_checked = now
    return account


def _fetch_custom_live(account: UsageAccount) -> UsageAccount:
    """Probe a custom LLM server or local harness (Ollama, LM Studio, vLLM)."""
    cred = (account.credential or "").strip()
    now = _get_utc_now_iso()
    base_url = (account.base_url or "http://localhost:11434").rstrip("/")

    account.plan_name = account.plan_name or "Custom Local LLM Harness"
    account.plan_label = "Local Harness"
    account.session_title = "Endpoint"
    account.weekly_title = None
    account.weekly_reset_time = None

    headers = {"Accept": "application/json"}
    if cred:
        headers["Authorization"] = f"Bearer {cred}"

    try:
        with httpx.Client(timeout=6.0) as client:
            for probe_path in ("/v1/models", "/models", "/api/tags", "/health"):
                try:
                    res = client.get(f"{base_url}{probe_path}", headers=headers)
                except Exception:
                    continue
                if res.status_code in (200, 204):
                    # A local harness enforces no quota, so there is nothing to
                    # measure beyond the fact that it answers.
                    account.status = "active"
                    account.error_message = None
                    account.session_reset_time = f"Online at {base_url}"
                    account.reset_time = account.session_reset_time
                    account.session_percent_used = None
                    account.session_percent_left = None
                    account.percent_used = None
                    account.last_checked = now
                    return account
                if res.status_code == 401:
                    account.status = "error"
                    account.error_message = "Authentication failed (invalid key or token)."
                    account.last_checked = now
                    return account
    except Exception as e:
        return _mark_unavailable(account, f"Connection failed: {e}")

    return _mark_unavailable(account, f"Could not reach the harness at {base_url}.")
