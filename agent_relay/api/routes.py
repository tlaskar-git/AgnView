"""FastAPI REST routes for AgentRelay."""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Dict, Any
import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from ..core.engine import (
    RelayEngine, NotFoundError, InvalidStateError, DependencyCycleError
)
from ..core.models import (
    Job, Task, CreateJobRequest, ClaimTaskRequest,
    CompleteTaskRequest, RequestRevisionRequest, RevisionFeedback, FailTaskRequest,
    TaskWaitResponse, AgentInstance, AgentHeartbeatRequest,
    UsageAccount, CreateUsageAccountRequest, UpdateUsageAccountRequest, DetectTokenRequest, UsageTelemetryPayload, ConsoleDispatchPayload,
    rows_carry_a_percentage
)
from ..core.prompts import format_web_prompt_for_agent
from ..core.usage_fetcher import fetch_account_usage, usage_is_stale
from ..core.pairing import (
    build_pairing_payload, generate_qr_svg, get_or_create_pairing_token,
    regenerate_pairing_token
)
from ..core.network import (
    CONNECTION_ORDER, LAN_CONNECT_TIMEOUT_SECONDS, available_transports,
    get_bind_mode, get_network_endpoints, get_resolved_transport_label,
    get_transport_label, resolve_hub_transport
)
from ..core import autostart
from ..core.config import ConfigError, DEFAULT_CONFIG_TEMPLATE, get_config_path, validate_relay_url
from ..core.iroh_transport import IrohTransport

router = APIRouter(prefix="/api")


def get_iroh_status(request: Request) -> Dict[str, Any]:
    """Return the iroh transport status, or a disabled stub when it is absent."""
    transport = getattr(request.app.state, "iroh", None)
    if transport is None:
        return {"name": "iroh", "state": "disabled", "error": None, "ticket": None}
    return transport.status()


def get_iroh_ticket(request: Request) -> Optional[str]:
    """Return the hub's iroh node ticket, or None while the transport is not up."""
    transport = getattr(request.app.state, "iroh", None)
    if transport is None:
        return None
    return transport.ticket


def build_transport_state(request: Request, port: int) -> Dict[str, Any]:
    """Describe the connection order and the transport a client lands on."""
    bind_mode = get_bind_mode()
    iroh_status = get_iroh_status(request)
    resolved = resolve_hub_transport(bind_mode, iroh_status)
    config = getattr(request.app.state, "config", None)

    return {
        "config": config.as_dict() if config is not None else None,
        "connection_order": list(CONNECTION_ORDER),
        "lan_timeout_ms": int(LAN_CONNECT_TIMEOUT_SECONDS * 1000),
        "resolved_transport": resolved,
        "resolved_transport_label": get_resolved_transport_label(resolved),
        "available_transports": available_transports(bind_mode, iroh_status),
        "bind_mode": bind_mode,
        "transport_label": get_transport_label(bind_mode),
        "endpoints": get_network_endpoints(port=port),
        "iroh": iroh_status
    }


def get_engine(request: Request) -> RelayEngine:
    return request.app.state.engine


# ----------------- Jobs -----------------

@router.get("/jobs", response_model=List[Job])
def list_jobs(request: Request):
    engine = get_engine(request)
    return engine.list_jobs()


@router.post("/jobs", response_model=Job)
def create_job(req: CreateJobRequest, request: Request):
    engine = get_engine(request)
    try:
        return engine.create_job(req)
    except (ValueError, DependencyCycleError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str, request: Request):
    engine = get_engine(request)
    try:
        return engine.get_job(job_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str, request: Request):
    engine = get_engine(request)
    deleted = engine.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
    return {"message": f"Job '{job_id}' deleted successfully."}


# ----------------- Tasks -----------------

@router.get("/tasks/{task_id}", response_model=Task)
def get_task(task_id: str, request: Request):
    engine = get_engine(request)
    try:
        return engine.get_task(task_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/tasks/{task_id}/claim", response_model=Task)
def claim_task(task_id: str, req: ClaimTaskRequest, request: Request):
    engine = get_engine(request)
    try:
        return engine.claim_task(task_id, req.agent, req.instance_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/tasks/{task_id}/complete", response_model=Task)
def complete_task(task_id: str, req: CompleteTaskRequest, request: Request):
    engine = get_engine(request)
    try:
        return engine.complete_task(
            task_id=task_id,
            summary=req.summary,
            artifacts=req.artifacts,
            agent=req.agent,
            instance_id=req.instance_id
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/tasks/{task_id}/fail", response_model=Task)
def fail_task(task_id: str, req: FailTaskRequest, request: Request):
    """Mark a task failed and block every task downstream of it."""
    engine = get_engine(request)
    reason = (req.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=422, detail="A failure reason is required.")
    try:
        return engine.fail_task(
            task_id=task_id,
            reason=reason,
            agent=req.agent,
            instance_id=req.instance_id
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except InvalidStateError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ----------------- Distributed Agent / Node Registry -----------------

@router.post("/agents/heartbeat", response_model=AgentInstance)
def agent_heartbeat(req: AgentHeartbeatRequest, request: Request):
    engine = get_engine(request)
    return engine.record_heartbeat(req)


@router.get("/agents")
def list_agents(request: Request, nodes: bool = Query(default=False), timeout_seconds: int = Query(default=60, ge=5, le=3600)):
    engine = get_engine(request)
    if nodes:
        return engine.list_agents(timeout_seconds=timeout_seconds)
    if hasattr(engine, "adapter_manager") and engine.adapter_manager:
        return engine.adapter_manager.get_public_adapters()
    return []


@router.post("/agents/reload")
def reload_agents(request: Request):
    engine = get_engine(request)
    if hasattr(engine, "adapter_manager") and engine.adapter_manager:
        engine.adapter_manager.reload()
        return {"status": "reloaded", "agents": [a.id for a in engine.adapter_manager.get_public_adapters()]}
    return {"status": "noop", "agents": []}


@router.post("/tasks/{task_id}/request-revision", response_model=RevisionFeedback)
def request_revision(task_id: str, req: RequestRevisionRequest, request: Request):
    engine = get_engine(request)
    try:
        return engine.request_revision(
            target_task_id=task_id,
            feedback=req.feedback,
            from_agent=req.from_agent
        )
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/tasks/{task_id}/wait-status", response_model=TaskWaitResponse)
def get_task_wait_status(task_id: str, request: Request):
    engine = get_engine(request)
    try:
        return engine.get_task_wait_status(task_id)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/tasks/{task_id}/wait", response_model=TaskWaitResponse)
async def wait_for_task_ready(
    task_id: str,
    request: Request,
    timeout_seconds: int = Query(default=60, ge=1, le=600)
):
    """
    Long-polling endpoint. Pauses execution until task is ready to start (all dependencies completed),
    or times out. Agents like Claude Code or Codex can use this or the CLI command `agent-relay wait`.
    """
    engine = get_engine(request)
    try:
        initial = engine.get_task_wait_status(task_id)
        if initial.ready:
            return initial

        # Listen to events until ready or timeout
        q = engine.subscribe_events()
        start_time = asyncio.get_event_loop().time()

        try:
            while True:
                remaining = timeout_seconds - (asyncio.get_event_loop().time() - start_time)
                if remaining <= 0:
                    return engine.get_task_wait_status(task_id)

                try:
                    await asyncio.wait_for(q.get(), timeout=min(remaining, 5.0))
                except asyncio.TimeoutError:
                    pass

                current = engine.get_task_wait_status(task_id)
                if current.ready:
                    return current
        finally:
            engine.unsubscribe_events(q)

    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ----------------- Claude.ai / ChatGPT Context Export -----------------

@router.get("/tasks/{task_id}/web-prompt")
def get_web_prompt(
    task_id: str,
    request: Request,
    target_llm: str = Query(default="claude.ai", description="claude.ai, chatgpt, or gemini (gemini.google.com)")
):
    engine = get_engine(request)
    try:
        task = engine.get_task(task_id)
        job = engine.get_job(task.job_id)
        prompt_text = format_web_prompt_for_agent(
            job=job,
            task=task,
            server_url=str(request.base_url).rstrip("/"),
            target_llm=target_llm
        )
        return {
            "task_id": task_id,
            "job_id": job.id,
            "target_llm": target_llm,
            "prompt": prompt_text
        }
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ----------------- Real-Time Server-Sent Events (SSE) -----------------

@router.get("/events")
async def sse_event_stream(request: Request):
    """Server-Sent Events endpoint for real-time live updates in the web dashboard."""
    engine = get_engine(request)
    q = engine.subscribe_events()

    async def event_generator():
        try:
            # Yield initial keep-alive, carrying the transport a client landed
            # on so a UI layer can render it without a second request.
            yield {
                "event": "connected",
                "data": json.dumps({
                    "status": "connected",
                    "transport": build_transport_state(request, port=engine.port)["resolved_transport"]
                })
            }
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield {
                        "event": event.get("event_type", "update"),
                        "data": json.dumps(event)
                    }
                except asyncio.TimeoutError:
                    # Heartbeat
                    yield {"event": "ping", "data": "{}"}
        finally:
            engine.unsubscribe_events(q)

    return EventSourceResponse(event_generator())


# ----------------- Notifications Configuration & Test -----------------

@router.get("/notifications")
def get_notifications(request: Request):
    """List configured notification channels with sensitive tokens masked."""
    engine = get_engine(request)
    if hasattr(engine, "notification_manager") and engine.notification_manager:
        return {"channels": engine.notification_manager.get_public_channels()}
    return {"channels": []}


@router.post("/notifications/test")
async def test_notification(request: Request, channel: Optional[str] = Query(default=None)):
    """Trigger a test notification to a channel or all channels."""
    engine = get_engine(request)
    if not hasattr(engine, "notification_manager") or not engine.notification_manager:
        raise HTTPException(status_code=500, detail="Notification manager not available")

    mgr = engine.notification_manager
    if channel:
        res = await mgr.test_channel(channel)
        return {"channel": res.channel, "success": res.success, "status_code": res.status_code, "error": res.error}
    else:
        results = await mgr.notify_event("test_alert", {
            "pipeline_id": "test",
            "pipeline_title": "AgnView Test Notification",
            "status": "completed",
            "duration": "0s"
        })
        return {"results": [{"channel": r.channel, "success": r.success, "status_code": r.status_code, "error": r.error} for r in results]}



# ----------------- Subscription Usage & Quotas (Claude, ChatGPT, Gemini) -----------------

@router.get("/usage/accounts", response_model=List[Dict[str, Any]])
def list_usage_accounts(request: Request, provider: Optional[str] = None):
    """List all tracked subscription accounts with masked credentials."""
    db = request.app.state.db
    raw_accounts = db.list_usage_accounts(provider=provider)
    masked_list = []
    for raw in raw_accounts:
        acc = UsageAccount(**raw)
        # Recompute on age, not on whether a row was ever filled in. Serving a
        # row that has figures but is old meant an account computed once by any
        # older version of the code was handed back unchanged for ever, so a fix
        # to how usage is counted or labelled never reached the page until
        # somebody clicked Refresh. The threshold is per provider: about a
        # minute for Claude Code, which is a local file walk, and fifteen
        # minutes for anything read over a rate-limited network endpoint. See
        # usage_fetcher.LOCAL_STALE_SECONDS and REMOTE_STALE_SECONDS.
        if usage_is_stale(acc):
            acc = fetch_account_usage(acc)
            db.save_usage_account(acc.model_dump())
        masked_list.append(acc.masked())
    return masked_list


@router.post("/usage/accounts", response_model=Dict[str, Any])
def add_usage_account(req: CreateUsageAccountRequest, request: Request):
    """Add a new subscription account and perform initial live quota fetch."""
    db = request.app.state.db
    account_id = req.id or f"{req.provider.lower()}-{uuid.uuid4().hex[:6]}"
    cred = req.get_credential()

    account = UsageAccount(
        id=account_id,
        provider=req.provider.lower(),
        name=req.name,
        auth_type=req.auth_type,
        credential=cred,
        org_id=req.org_id,
        plan_name=req.plan_name or "Pro",
        plan_label=req.plan_label,
        session_title=req.session_title,
        session_reset_time=req.session_reset_time,
        session_percent_used=req.session_percent_used,
        session_percent_left=req.session_percent_left,
        weekly_title=req.weekly_title,
        weekly_reset_time=req.weekly_reset_time,
        weekly_percent_used=req.weekly_percent_used,
        weekly_percent_left=req.weekly_percent_left,
        weekly_breakdown=req.weekly_breakdown,
        session_breakdown=req.session_breakdown,
        tokens_limit=req.tokens_limit,
        cost_limit_usd=req.cost_limit_usd,
        base_url=req.base_url
    )

    # Perform initial live fetch
    account = fetch_account_usage(account)
    db.save_usage_account(account.model_dump())

    engine = get_engine(request)
    engine._sync_broadcast("global", "usage_account_added", {
        "id": account.id,
        "provider": account.provider,
        "name": account.name,
        "status": account.status
    })

    return account.masked()


@router.get("/usage/accounts/{account_id}", response_model=Dict[str, Any])
def get_usage_account(account_id: str, request: Request):
    """Get single subscription account details for editing."""
    db = request.app.state.db
    raw = db.get_usage_account(account_id)
    if not raw:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
    account = UsageAccount(**raw)
    return account.masked()


@router.put("/usage/accounts/{account_id}", response_model=Dict[str, Any])
def update_usage_account(account_id: str, req: UpdateUsageAccountRequest, request: Request):
    """Update subscription account fields (name, plan, credential/token, base_url) and re-probe."""
    db = request.app.state.db
    raw = db.get_usage_account(account_id)
    if not raw:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")

    account = UsageAccount(**raw)
    if req.name is not None and req.name.strip():
        account.name = req.name.strip()
    if req.plan_name is not None and req.plan_name.strip():
        account.plan_name = req.plan_name.strip()
    if req.plan_label is not None and req.plan_label.strip():
        account.plan_label = req.plan_label.strip()
    if req.auth_type is not None and req.auth_type.strip():
        account.auth_type = req.auth_type.strip()
    if req.base_url is not None:
        account.base_url = req.base_url.strip() or None
    if req.session_title is not None:
        account.session_title = req.session_title
    if req.session_reset_time is not None:
        account.session_reset_time = req.session_reset_time
        account.reset_time = req.session_reset_time
    if req.session_percent_used is not None:
        account.session_percent_used = req.session_percent_used
        account.percent_used = req.session_percent_used
    if req.session_percent_left is not None:
        account.session_percent_left = req.session_percent_left
    if req.weekly_title is not None:
        account.weekly_title = req.weekly_title
    if req.weekly_reset_time is not None:
        account.weekly_reset_time = req.weekly_reset_time
    if req.weekly_percent_used is not None:
        account.weekly_percent_used = req.weekly_percent_used
    if req.weekly_percent_left is not None:
        account.weekly_percent_left = req.weekly_percent_left
    if req.weekly_breakdown is not None:
        account.weekly_breakdown = req.weekly_breakdown
    if req.session_breakdown is not None:
        account.session_breakdown = req.session_breakdown

    new_cred = req.get_credential()
    if new_cred:
        account.credential = new_cred

    # Re-probe live usage with updated credentials
    account = fetch_account_usage(account)
    db.save_usage_account(account.model_dump())

    engine = get_engine(request)
    engine._sync_broadcast("global", "usage_account_updated", {
        "id": account.id,
        "name": account.name,
        "status": account.status,
        "percent_used": account.percent_used
    })

    return account.masked()


@router.post("/usage/accounts/{account_id}/telemetry", response_model=Dict[str, Any])
def sync_account_telemetry(account_id: str, payload: UsageTelemetryPayload, request: Request):
    """Directly update detailed dual-limit telemetry (e.g. from browser extension or console snippet)."""
    db = request.app.state.db
    raw = db.get_usage_account(account_id)
    if not raw:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
    acc = UsageAccount(**raw)
    # A percentage arriving here was read off the provider's own usage page, so
    # it is the real share of a real window. Stamp the window it belongs to, so
    # the next local recompute preserves it instead of blanking it and dropping
    # back to a token count. Only a window that actually carried a percentage
    # gets stamped.
    synced_at = datetime.now(timezone.utc).isoformat()
    # A per-group breakdown counts as a measurement of its window just as a
    # single top-level percentage does. Antigravity reports only per-group
    # figures, so without this its sync would go unstamped and the next
    # recompute would blank it.
    if (
        payload.session_percent_used is not None
        or payload.session_percent_left is not None
        or rows_carry_a_percentage(payload.session_breakdown)
    ):
        acc.session_telemetry_synced_at = synced_at
    if (
        payload.weekly_percent_used is not None
        or payload.weekly_percent_left is not None
        or rows_carry_a_percentage(payload.weekly_breakdown)
    ):
        acc.weekly_telemetry_synced_at = synced_at
    if payload.plan_name:
        acc.plan_name = payload.plan_name
    if payload.plan_label:
        acc.plan_label = payload.plan_label
    if payload.session_title:
        acc.session_title = payload.session_title
    if payload.session_reset_time:
        acc.session_reset_time = payload.session_reset_time
        acc.reset_time = payload.session_reset_time
    if payload.session_percent_used is not None:
        acc.session_percent_used = payload.session_percent_used
        acc.percent_used = payload.session_percent_used
        acc.session_percent_left = round(100.0 - payload.session_percent_used, 1)
    if payload.session_percent_left is not None:
        acc.session_percent_left = payload.session_percent_left
    if payload.weekly_title:
        acc.weekly_title = payload.weekly_title
    if payload.weekly_reset_time:
        acc.weekly_reset_time = payload.weekly_reset_time
    if payload.weekly_percent_used is not None:
        acc.weekly_percent_used = payload.weekly_percent_used
        acc.weekly_percent_left = round(100.0 - payload.weekly_percent_used, 1)
    if payload.weekly_percent_left is not None:
        acc.weekly_percent_left = payload.weekly_percent_left
    if payload.weekly_breakdown is not None:
        acc.weekly_breakdown = payload.weekly_breakdown
    if payload.session_breakdown is not None:
        acc.session_breakdown = payload.session_breakdown
    if payload.tokens_limit is not None:
        acc.tokens_limit = payload.tokens_limit
    if payload.tokens_used is not None:
        acc.tokens_used = payload.tokens_used
        acc.tokens_remaining = (
            max(0, acc.tokens_limit - acc.tokens_used) if acc.tokens_limit is not None else None
        )

    acc.last_checked = datetime.now(timezone.utc).isoformat()
    acc.status = "active"
    acc.error_message = None

    db.save_usage_account(acc.model_dump())
    engine = get_engine(request)
    engine._sync_broadcast("global", "usage_account_telemetry_updated", {
        "id": acc.id,
        "name": acc.name,
        "session_percent_used": acc.session_percent_used,
        "weekly_percent_used": acc.weekly_percent_used
    })
    return acc.masked()


@router.post("/usage/detect-local-token")
def detect_local_token(
    req: Optional[DetectTokenRequest] = None,
    provider: Optional[str] = Query(None, description="Provider to scan for"),
    request: Request = None
):
    """Scan local machine for pre-configured CLI tokens, environment variables, or running local harnesses."""
    target_provider = (req.provider if req and req.provider else provider or "").lower().strip()
    detected = False
    token = ""
    source = ""
    base_url = None
    name_hint = None
    plan_hint = None

    if "claude" in target_provider or "anthropic" in target_provider:
        env_val = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
        if env_val:
            detected, token, source = True, env_val, "Environment variable ($ANTHROPIC_API_KEY)"
        else:
            candidates = [
                Path.home() / ".claude.json",
                Path.home() / ".config" / "claude" / "keys.json",
                Path.home() / ".anthropic" / "credentials",
            ]
            for p in candidates:
                if p.exists():
                    try:
                        content = p.read_text(encoding="utf-8")
                        j = json.loads(content)
                        k = j.get("apiKey") or j.get("api_key") or j.get("sessionKey")
                        if k:
                            detected, token, source = True, k, f"Local file ({p.name})"
                            break
                        oa = j.get("oauthAccount")
                        if isinstance(oa, dict) and oa.get("emailAddress"):
                            org_type = oa.get("organizationType", "claude_max").replace("_", " ").title()
                            display_name = oa.get("displayName") or oa.get("emailAddress", "").split("@")[0]
                            tier = oa.get("organizationRateLimitTier") or "20x"
                            detected = True
                            token = f"claude-cli-{oa.get('accountUuid', 'oauth')[:8]}"
                            source = f"Claude Code CLI ({display_name} · {org_type})"
                            name_hint = f"{display_name} · Max"
                            plan_hint = f"{org_type} ({tier})"
                            break
                    except Exception:
                        pass

    elif "chatgpt" in target_provider or "openai" in target_provider or "codex" in target_provider:
        env_val = os.environ.get("OPENAI_API_KEY")
        if env_val:
            detected, token, source = True, env_val, "Environment variable ($OPENAI_API_KEY)"
            name_hint, plan_hint = "OpenAI API", "GPT-4o / Codex"
        else:
            codex_auth = Path.home() / ".codex" / "auth.json"
            if codex_auth.exists():
                try:
                    cj = json.loads(codex_auth.read_text(encoding="utf-8"))
                    toks = cj.get("tokens") or {}
                    access_tok = toks.get("access_token") or cj.get("OPENAI_API_KEY")
                    if access_tok:
                        detected, token, source = True, access_tok, "Local Codex / ChatGPT CLI (auth.json)"
                        name_hint = "ChatGPT Plus (Codex)"
                        plan_hint = "ChatGPT Plus / GPT-4o"
                except Exception:
                    pass

            if not detected:
                p = Path.home() / ".config" / "openai" / "credentials"
                if p.exists():
                    try:
                        c = p.read_text(encoding="utf-8").strip()
                        if c:
                            detected, token, source = True, c, f"Local file ({p.name})"
                            name_hint, plan_hint = "OpenAI API", "Standard"
                    except Exception:
                        pass

    elif "gemini" in target_provider or "google" in target_provider:
        env_val = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if env_val:
            detected, token, source = True, env_val, "Environment variable ($GEMINI_API_KEY)"
            name_hint, plan_hint = "Google Gemini", "Gemini 1.5 Pro"
        else:
            gemini_oauth = Path.home() / ".gemini" / "oauth_creds.json"
            if gemini_oauth.exists():
                try:
                    gj = json.loads(gemini_oauth.read_text(encoding="utf-8"))
                    access_tok = gj.get("access_token")
                    if access_tok:
                        email = "User"
                        acc_file = Path.home() / ".gemini" / "google_accounts.json"
                        if acc_file.exists():
                            try:
                                email = json.loads(acc_file.read_text(encoding="utf-8")).get("active", "User")
                            except Exception:
                                pass
                        detected, token, source = True, access_tok, f"Local Gemini CLI ({email})"
                        name_hint = f"Gemini Pro ({email.split('@')[0]})"
                        plan_hint = "Gemini Advanced / Pro"
                except Exception:
                    pass

    elif "deepseek" in target_provider:
        env_val = os.environ.get("DEEPSEEK_API_KEY")
        if env_val:
            detected, token, source = True, env_val, "Environment variable ($DEEPSEEK_API_KEY)"
            name_hint, plan_hint = "DeepSeek Cloud V3", "DeepSeek-V3 / R1"
        else:
            for p in [Path.home() / ".deepseek" / "credentials", Path.home() / ".config" / "deepseek" / "credentials"]:
                if p.exists():
                    try:
                        c = p.read_text(encoding="utf-8").strip()
                        if c:
                            detected, token, source = True, c, f"Local file ({p.name})"
                            name_hint, plan_hint = "DeepSeek Cloud", "DeepSeek-V3 / R1"
                            break
                    except Exception:
                        pass

    elif "custom" in target_provider or "ollama" in target_provider:
        try:
            r = httpx.get("http://localhost:11434/api/tags", timeout=1.0)
            if r.status_code == 200:
                detected, token, source, base_url = True, "local-ollama", "Local Ollama server active", "http://localhost:11434"
                name_hint, plan_hint = "Local Ollama Harness", "Llama / Qwen / DeepSeek-R1"
        except Exception:
            pass

    # A credential read from a CLI's own OAuth session (Claude Code, the Codex
    # CLI, the Gemini CLI) is a session token, not the provider's API key, and
    # the Add Account form must say so, or the account is saved with the wrong
    # authentication method even though the credential itself is correct.
    auth_type_hint = "session_token" if source and "CLI" in source else "api_key"

    return {
        "found": detected,
        "detected": detected,
        "provider": target_provider,
        "token": token,
        "source": source,
        "details": source,
        "base_url": base_url,
        "name": name_hint,
        "plan": plan_hint,
        "auth_type": auth_type_hint if detected else None
    }



@router.delete("/usage/accounts/{account_id}")
def delete_usage_account(account_id: str, request: Request):
    """Remove a tracked subscription account."""
    db = request.app.state.db
    deleted = db.delete_usage_account(account_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")
    return {"message": f"Account '{account_id}' removed.", "deleted": True, "id": account_id}


@router.post("/usage/accounts/{account_id}/refresh", response_model=Dict[str, Any])
def refresh_usage_account(account_id: str, request: Request):
    """Pull fresh live usage data directly from provider for this account."""
    db = request.app.state.db
    raw = db.get_usage_account(account_id)
    if not raw:
        raise HTTPException(status_code=404, detail=f"Account '{account_id}' not found.")

    account = UsageAccount(**raw)
    account = fetch_account_usage(account)
    db.save_usage_account(account.model_dump())

    engine = get_engine(request)
    engine._sync_broadcast("global", "usage_refreshed", {
        "id": account.id,
        "provider": account.provider,
        "percent_used": account.percent_used
    })

    return account.masked()


@router.post("/usage/refresh-all", response_model=List[Dict[str, Any]])
def refresh_all_usage_accounts(request: Request):
    """Pull fresh live usage data directly from all provider sites for all accounts."""
    db = request.app.state.db
    raw_accounts = db.list_usage_accounts()
    refreshed_list = []

    for raw in raw_accounts:
        acc = UsageAccount(**raw)
        acc = fetch_account_usage(acc)
        db.save_usage_account(acc.model_dump())
        refreshed_list.append(acc.masked())

    engine = get_engine(request)
    engine._sync_broadcast("global", "usage_all_refreshed", {
        "total_refreshed": len(refreshed_list)
    })

    return refreshed_list


# ----------------- Interactive Multi-Agent Console Endpoints -----------------

@router.post("/console/dispatch")
async def dispatch_console_command(payload: ConsoleDispatchPayload, request: Request):
    """Dispatch prompt/command directly to local Claude Code, Codex, or AntiGravity."""
    engine = get_engine(request)

    # session_id groups messages in the UI. The client sends back the id it
    # already holds for this target, so a follow-up message stays in the same
    # thread. A fresh id is minted only for a genuinely new conversation: the
    # first one, or one the operator started with "New chat". The real CLI
    # resume token is tracked separately, per agent and working directory.
    if payload.reset_session or not payload.session_id:
        session_id = f"sess-{uuid.uuid4().hex[:8]}"
    else:
        session_id = payload.session_id

    # Run dispatch in background task
    asyncio.create_task(
        engine.runner.dispatch(
            agent=payload.agent,
            prompt=payload.prompt,
            cwd=payload.working_directory,
            session_id=session_id,
            model=payload.model,
            effort=payload.effort,
            files=payload.files,
            skill=payload.skill,
            reset_session=payload.reset_session
        )
    )

    return {
        "status": "dispatched",
        "agent": payload.agent,
        "session_id": session_id,
        "message": f"Instruction dispatched to {payload.agent}."
    }


@router.post("/console/session/reset")
async def reset_console_session(
    request: Request,
    agent: str = Query(..., description="Dispatch target whose conversation is being reset"),
    working_directory: Optional[str] = Query(None, description="Directory the target runs in"),
):
    """Forget a target's conversation and end the CLI process still holding it.

    "New chat" calls this so a fresh conversation really starts fresh, instead
    of waiting for the next dispatch to drop the stored resume id.
    """
    engine = get_engine(request)
    result = await engine.runner.reset_session(agent, working_directory)
    return {"status": "reset", **result}


@router.get("/console/live-sessions", response_model=List[Dict[str, Any]])
def list_live_sessions(request: Request):
    """List the CLI processes AgnView is currently holding open.

    These are the sessions a dispatch started and kept alive, one per agent
    and working directory. The Sessions view lists them so an operator can see
    what is running and carry on talking to it, instead of having to guess
    from the console that a process is still there.

    Externally started CLI sessions are not in this registry and are not
    reported here.
    """
    engine = get_engine(request)
    moment = time.monotonic()
    sessions = [
        session.describe(moment)
        for session in list(engine.runner.live_sessions.values())
    ]
    # Busiest first, then most recently active, so the one being worked on is
    # at the top of the list.
    sessions.sort(key=lambda s: (not s["busy"], s["idle_seconds"]))
    return sessions


@router.get("/console/logs", response_model=List[Dict[str, Any]])
def get_console_logs(
    request: Request,
    agent: Optional[str] = Query("all", description="Filter by agent role or 'all'"),
    limit: int = Query(250, description="Max lines to retrieve"),
    session_id: Optional[str] = Query(None, description="Optional session filter"),
    after_id: Optional[int] = Query(None, description="Return only entries newer than this row id")
):
    """Retrieve historical console logs.

    The dashboard hydrates once on load and then follows the SSE stream, so
    this is a backfill endpoint rather than a poll. Pass ``after_id`` to fetch
    only what arrived after the newest entry the client already holds.
    """
    engine = get_engine(request)
    return engine.db.get_console_logs(
        agent=agent, limit=limit, session_id=session_id, after_id=after_id
    )


@router.post("/console/clear")
def clear_console_logs(request: Request, agent: Optional[str] = Query(None)):
    """Clear console logs buffer."""
    engine = get_engine(request)
    engine.db.clear_console_logs(agent=agent)
    engine._sync_broadcast("console", "console_cleared", {"agent": agent or "all"})
    return {"status": "cleared", "agent": agent or "all"}


# ----------------- System Capabilities & Skills Endpoints -----------------

@router.get("/system/capabilities")
def get_system_capabilities(request: Request):
    """Detect local agent CLIs, skills, models, browser profiles, and working directories."""
    import shutil

    engine = get_engine(request)

    # 1. Detect Installed CLI Agents
    installed_agents = {}
    for agent_key, exe_names in [
        ("claude_code", ["claude", "claude.cmd"]),
        ("codex", ["codex", "codex.cmd"]),
        ("antigravity", ["agy", "agy.exe", "antigravity"]),
        ("ollama", ["ollama", "ollama.exe"])
    ]:
        found_path = None
        for name in exe_names:
            p = shutil.which(name)
            if p:
                found_path = p
                break
        # Also check standard user installation locations
        if not found_path and os.name == "nt":
            custom_locations = [
                os.path.expanduser(rf"~\AppData\Local\agy\bin\{exe_names[0]}.exe"),
                os.path.expanduser(rf"~\AppData\Roaming\npm\{exe_names[0]}.cmd"),
                os.path.expanduser(rf"~\AppData\Local\Programs\{exe_names[0]}\{exe_names[0]}.exe"),
            ]
            for cl in custom_locations:
                if os.path.exists(cl):
                    found_path = cl
                    break

        installed_agents[agent_key] = {
            "installed": bool(found_path),
            "path": found_path
        }

    # 2. Detect Connected Accounts
    accounts = engine.db.list_usage_accounts()
    connected_providers = set(a["provider"] for a in accounts)

    # 3. Discover Local Skills & Slash Commands
    skills = [
        {"name": "/goal", "description": "Autonomous end-to-end task completion", "type": "slash_command"},
        {"name": "/review", "description": "Review code diffs and PR quality", "type": "slash_command"},
        {"name": "/plan", "description": "Generate comprehensive implementation plan", "type": "slash_command"},
        {"name": "/help", "description": "Show agent usage and capabilities", "type": "slash_command"},
        {"name": "/compact", "description": "Compact context window", "type": "slash_command"},
        {"name": "/cost", "description": "Display token cost breakdown", "type": "slash_command"}
    ]

    # Scan Claude skills (~/.claude/skills)
    claude_skills_dir = Path.home() / ".claude" / "skills"
    if claude_skills_dir.exists():
        try:
            for item in sorted(claude_skills_dir.iterdir()):
                if item.is_dir() and not item.name.startswith("."):
                    skills.append({
                        "name": item.name,
                        "description": f"Claude Code local skill ({item.name})",
                        "type": "claude_skill"
                    })
        except Exception:
            pass

    # Scan AntiGravity skills (~/.gemini/antigravity/builtin/skills)
    agy_skills_dir = Path.home() / ".gemini" / "antigravity" / "builtin" / "skills"
    if agy_skills_dir.exists():
        try:
            for item in sorted(agy_skills_dir.iterdir()):
                if item.is_dir() and not item.name.startswith("."):
                    skills.append({
                        "name": item.name,
                        "description": f"AntiGravity builtin skill ({item.name})",
                        "type": "agy_skill"
                    })
        except Exception:
            pass

    # 4. Supported Models & Efforts matching Desktop Apps & CLIs (Latest 2026/2027)
    models = {
        "claude_code": [
            {"id": "claude-fable-5-1", "name": "Fable 5.1"},
            {"id": "claude-fable-5", "name": "Fable 5"},
            {"id": "claude-opus-5", "name": "Opus 5"},
            {"id": "claude-opus-4-8", "name": "Opus 4.8"},
            {"id": "claude-opus-4-7", "name": "Opus 4.7"},
            {"id": "claude-opus-4-6-thinking", "name": "Opus 4.6"},
            {"id": "claude-sonnet-5", "name": "Sonnet 5"},
            {"id": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
            {"id": "claude-3-7-sonnet-20250219", "name": "Sonnet 3.7"},
            {"id": "claude-3-5-sonnet-20241022", "name": "Sonnet 3.5"},
            {"id": "claude-3-5-haiku-20241022", "name": "Haiku 3.5"},
            {"id": "claude-3-opus-20240229", "name": "Claude 3 Opus"}
        ],
        "codex": [
            {"id": "gpt-6-astra", "name": "GPT-6 Astra"},
            {"id": "gpt-5-codex", "name": "GPT-5 Codex"},
            {"id": "gpt-5", "name": "GPT-5"},
            {"id": "o3-mini", "name": "o3-mini"},
            {"id": "o3", "name": "o3"},
            {"id": "o1", "name": "o1"},
            {"id": "o1-mini", "name": "o1-mini"},
            {"id": "gpt-4.5-preview", "name": "GPT-4.5 Preview"},
            {"id": "gpt-4o", "name": "GPT-4o"},
            {"id": "gpt-4o-mini", "name": "GPT-4o-mini"}
        ],
        "antigravity": [
            {"id": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
            {"id": "gemini-3.7-flash", "name": "Gemini 3.7 Flash"},
            {"id": "gemini-3.6-flash", "name": "Gemini 3.6 Flash"},
            {"id": "gemini-3.1-pro", "name": "Gemini 3.1 Pro"},
            {"id": "gemini-2.5-pro", "name": "Gemini 2.5 Pro"},
            {"id": "gemini-2.5-flash", "name": "Gemini 2.5 Flash"},
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6"},
            {"id": "claude-opus-4-6-thinking", "name": "Claude Opus 4.6"},
            {"id": "gpt-oss-120b", "name": "GPT-OSS 120B"}
        ],
        "all": [
            {"id": "auto", "name": "Auto"}
        ]
    }

    efforts_by_provider = {
        "claude_code": [
            {"id": "default", "name": "Default"},
            {"id": "low", "name": "Low Effort"},
            {"id": "medium", "name": "Medium Effort"},
            {"id": "high", "name": "High Effort"},
            {"id": "xhigh", "name": "Extra High Effort"},
            {"id": "max", "name": "Maximum Effort"}
        ],
        "codex": [
            {"id": "default", "name": "Default"},
            {"id": "low", "name": "Low Reasoning"},
            {"id": "medium", "name": "Medium Reasoning"},
            {"id": "high", "name": "High Reasoning"},
            {"id": "max", "name": "Maximum Reasoning"}
        ],
        "antigravity": [
            {"id": "default", "name": "Default"},
            {"id": "low", "name": "Low Reasoning"},
            {"id": "medium", "name": "Medium Reasoning"},
            {"id": "high", "name": "High Reasoning"}
        ],
        "all": [
            {"id": "default", "name": "Auto"},
            {"id": "high", "name": "High Reasoning"}
        ]
    }

    slash_commands_by_provider = {
        "claude_code": [
            {"name": "/help", "description": "Show capabilities, CLI options, and usage guide"},
            {"name": "/doctor", "description": "Health check, environment diagnostics, and fix issues"},
            {"name": "/compact", "description": "Compact conversation context into a concise summary"},
            {"name": "/cost", "description": "Display token usage, breakdown, and cumulative session cost"},
            {"name": "/clear", "description": "Clear current conversation context"},
            {"name": "/config", "description": "Inspect and update settings and tool permissions"},
            {"name": "/pr", "description": "Create pull request with AI-generated branch, title, and body"},
            {"name": "/review", "description": "Deep multi-agent code review of branch or changes"},
            {"name": "/init", "description": "Initialize CLAUDE.md project guide and conventions"},
            {"name": "/memory", "description": "View and edit persistent project memory"},
            {"name": "/bug", "description": "Report a bug or issue with reproduction details"},
            {"name": "/login", "description": "Authenticate with Anthropic account"},
            {"name": "/logout", "description": "Sign out of Anthropic account"},
            {"name": "/resume", "description": "Resume a previous background session"},
            {"name": "/terminal-setup", "description": "Set up shift+enter and terminal keybindings"},
            {"name": "/ultrareview", "description": "Run cloud-hosted multi-agent code review"}
        ],
        "codex": [
            {"name": "/plan", "description": "Generate structured step-by-step implementation plan"},
            {"name": "/review", "description": "Comprehensive code quality, bugs, and security review"},
            {"name": "/test", "description": "Generate comprehensive unit tests and edge cases"},
            {"name": "/fix", "description": "Diagnose and fix errors in target files"},
            {"name": "/diff", "description": "Inspect and verify git code diffs before applying"},
            {"name": "/refactor", "description": "Clean architecture refactoring without behavior change"},
            {"name": "/explain", "description": "Explain code structure, algorithms, and logic"},
            {"name": "/clear", "description": "Clear conversation context buffer"},
            {"name": "/reset", "description": "Reset workspace state and chat history"},
            {"name": "/help", "description": "Display Codex command reference and shortcuts"}
        ],
        "antigravity": [
            {"name": "/goal", "description": "Autonomous end-to-end task completion (long-running)"},
            {"name": "/boost", "description": "Deep strategic planning with multiple perspective verification"},
            {"name": "/plan", "description": "Create comprehensive implementation plan artifact"},
            {"name": "/grill-me", "description": "Interactive interview to resolve design decisions"},
            {"name": "/teamwork-preview", "description": "Orchestrate team of autonomous subagents"},
            {"name": "/browser", "description": "Launch browser agent for web research and UI verification"},
            {"name": "/learn", "description": "Persist custom behavioral rules and user preferences"},
            {"name": "/schedule", "description": "Schedule one-shot timer or recurring cron job"},
            {"name": "/review", "description": "Code review and automated lint inspection"},
            {"name": "/skills", "description": "List, inspect, and discover installed Antigravity skills"},
            {"name": "/clear", "description": "Clear active context and stream buffer"},
            {"name": "/help", "description": "Display Google Antigravity guide, CLI commands, and keybindings"}
        ],
        "all": [
            {"name": "/plan", "description": "Multi-agent coordinated execution plan"},
            {"name": "/review", "description": "Cross-agent multi-perspective code review"},
            {"name": "/goal", "description": "Autonomous goal dispatch to all connected agents"},
            {"name": "/clear", "description": "Clear all agent console buffers"},
            {"name": "/help", "description": "Show unified agent capabilities"}
        ]
    }

    # 5. Working Directory & Detected Project Paths
    cwd = os.getcwd()
    recent_paths = [cwd]
    parent = os.path.dirname(cwd)
    if parent and os.path.exists(parent):
        try:
            for d in os.listdir(parent):
                full = os.path.join(parent, d)
                if os.path.isdir(full) and not d.startswith(".") and full not in recent_paths:
                    recent_paths.append(full)
                    if len(recent_paths) >= 8:
                        break
        except Exception:
            pass

    # Default workspace per provider defaults to the current working directory
    default_cwd_by_provider = {
        "claude_code": cwd,
        "codex": cwd,
        "antigravity": cwd
    }
    default_cwd = cwd

    # 6. Autostart Status, from the registration the CLI actually writes
    try:
        autostart_enabled = autostart.status()
    except Exception:
        autostart_enabled = False

    installed_clis = [
        {"id": "claude_code", "name": "Claude Code", "available": installed_agents.get("claude_code", {}).get("installed", False)},
        {"id": "codex", "name": "Codex / ChatGPT", "available": installed_agents.get("codex", {}).get("installed", False)},
        {"id": "antigravity", "name": "AntiGravity (AGY)", "available": installed_agents.get("antigravity", {}).get("installed", False)},
    ]

    return {
        "installed_agents": installed_agents,
        "installed_clis": installed_clis,
        "connected_providers": list(connected_providers),
        "total_accounts": len(accounts),
        "skills": skills,
        "models": models,
        "efforts": ["low", "medium", "high"],
        "efforts_by_provider": efforts_by_provider,
        "slash_commands_by_provider": slash_commands_by_provider,
        "current_cwd": cwd,
        "default_cwd": default_cwd,
        "default_cwd_by_provider": default_cwd_by_provider,
        "recent_paths": recent_paths,
        "autostart_enabled": autostart_enabled
    }


@router.get("/system/files")
def list_workspace_files(request: Request, cwd: Optional[str] = None):
    """List project files in CWD for attachment selector."""
    target_dir = cwd or os.getcwd()
    file_list = []
    if not os.path.exists(target_dir):
        return {"files": []}

    ignored_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".idea", ".vscode", "dist", "build"}
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = [d for d in dirs if d not in ignored_dirs and not d.startswith(".")]
        rel_root = os.path.relpath(root, target_dir)
        for f in files:
            if f.startswith(".") or f.endswith((".pyc", ".log", ".png", ".jpg", ".jpeg", ".db")):
                continue
            rel_path = f if rel_root == "." else os.path.join(rel_root, f).replace("\\", "/")
            file_list.append(rel_path)
            if len(file_list) >= 60:
                break
        if len(file_list) >= 60:
            break

    return {"files": sorted(file_list), "cwd": target_dir}


@router.get("/prompts/saved")
def get_saved_prompts(request: Request):
    """List user saved quick prompts."""
    engine = get_engine(request)
    prompts = engine.db.list_saved_prompts()
    if not prompts:
        # Seed default quick prompts if table is empty
        defaults = [
            {"id": "p-1", "title": "Run test suite", "prompt": "Run pytest tests and verify all pass", "category": "testing"},
            {"id": "p-2", "title": "Review git diff", "prompt": "Review current git diff and summarize status", "category": "git"},
            {"id": "p-3", "title": "Inspect upstream task", "prompt": "Check Task A outputs and inspect authentication schema", "category": "review"},
            {"id": "p-4", "title": "FastAPI CRUD synthesis", "prompt": "Synthesize FastAPI endpoints with pydantic v2 schemas", "category": "dev"}
        ]
        for d in defaults:
            engine.db.save_saved_prompt(id=d["id"], title=d["title"], prompt=d["prompt"], category=d["category"])
        prompts = engine.db.list_saved_prompts()
    return prompts


@router.post("/prompts/saved")
def create_saved_prompt(req: Dict[str, Any], request: Request):
    """Save a new user prompt."""
    engine = get_engine(request)
    prompt_id = req.get("id") or f"p-{uuid.uuid4().hex[:6]}"
    title = req.get("title", "").strip() or "Quick Prompt"
    prompt = req.get("prompt", "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt text cannot be empty.")
    saved = engine.db.save_saved_prompt(id=prompt_id, title=title, prompt=prompt, category=req.get("category", "custom"))
    return saved


@router.delete("/prompts/saved/{prompt_id}")
def delete_saved_prompt(prompt_id: str, request: Request):
    """Delete a saved prompt."""
    engine = get_engine(request)
    deleted = engine.db.delete_saved_prompt(prompt_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Prompt not found.")
    return {"message": "Prompt removed.", "id": prompt_id}


@router.get("/system/autostart")
def get_autostart_status():
    """Report whether AgnView is registered to start at login.

    Reads the same registration the CLI writes. It used to look for a .cmd
    file in the Windows Startup folder that only this route ever wrote, so it
    reported autostart off on every normal install, where the CLI had already
    registered it.
    """
    return {
        "enabled": autostart.status(),
        "command": autostart.registered_command(),
        "os": os.name,
    }


@router.post("/system/autostart")
def toggle_autostart(req: Dict[str, bool]):
    """Turn autostart at login on or off, on every platform.

    Off is remembered, so the next `agnview serve` leaves it off rather than
    registering it again. On Linux and macOS this route used to change nothing
    at all and report success anyway.
    """
    enable = req.get("enable", True)
    try:
        if enable:
            message = autostart.enable_and_clear_opt_out()
        else:
            message = autostart.disable_and_remember_opt_out()
    except Exception as e:
        return {"success": False, "enabled": autostart.status(), "message": str(e)}
    return {"success": True, "enabled": autostart.status(), "message": message}



# ----------------- Mobile Client Pairing & Local Endpoints -----------------

@router.get("/mobile/pairing")
def get_mobile_pairing_info(request: Request):
    """Return local RFC1918 and loopback endpoints, pairing token, and SVG QR code for iOS/Android apps."""
    engine = get_engine(request)
    port = engine.port
    endpoints = get_network_endpoints(port=port)
    token = request.app.state.auth_token or get_or_create_pairing_token()

    primary_url = endpoints.get("lan") or endpoints.get("localhost") or f"http://127.0.0.1:{port}"
    payload = build_pairing_payload(
        primary_url=primary_url,
        endpoints=endpoints,
        token=token,
        port=port,
        iroh_ticket=get_iroh_ticket(request)
    )

    qr_svg = generate_qr_svg(payload["deeplink"])

    return {
        "success": True,
        "pairing": payload,
        "pairing_token": token,
        "deep_link": payload["deeplink"],
        "active_endpoint": primary_url,
        "endpoints": endpoints,
        "bind_mode": get_bind_mode(),
        "transport_label": get_transport_label(),
        "qr_svg": qr_svg
    }


@router.post("/mobile/pairing/regenerate")
def regenerate_mobile_pairing(request: Request):
    """Regenerate pairing token and pair ID, invalidating previously paired devices."""
    engine = get_engine(request)
    port = engine.port
    endpoints = get_network_endpoints(port=port)
    new_pair_id, new_token = regenerate_pairing_token()
    request.app.state.auth_token = new_token

    primary_url = endpoints.get("lan") or endpoints.get("localhost") or f"http://127.0.0.1:{port}"
    payload = build_pairing_payload(
        primary_url=primary_url,
        endpoints=endpoints,
        token=new_token,
        port=port,
        iroh_ticket=get_iroh_ticket(request)
    )
    qr_svg = generate_qr_svg(payload["deeplink"])

    return {
        "success": True,
        "pairing": payload,
        "pairing_token": new_token,
        "pair_id": new_pair_id,
        "deep_link": payload["deeplink"],
        "active_endpoint": primary_url,
        "endpoints": endpoints,
        "bind_mode": get_bind_mode(),
        "transport_label": get_transport_label(),
        "qr_svg": qr_svg
    }


@router.get("/mobile/status")
def get_mobile_status(request: Request):
    """Check local connectivity status for mobile apps."""
    engine = get_engine(request)
    endpoints = get_network_endpoints(port=engine.port)
    transport = build_transport_state(request, port=engine.port)
    return {
        "app": "AgnView",
        "status": "healthy",
        "endpoints": endpoints,
        "bind_mode": get_bind_mode(),
        "transport_label": get_transport_label(),
        "resolved_transport": transport["resolved_transport"],
        "iroh": transport["iroh"]
    }


# ----------------- Transport -----------------

@router.get("/transport")
def get_transport_state(request: Request):
    """Report the connection order and the transport a client lands on.

    A client walks connection_order and stops at the first rung that answers,
    giving the LAN rung lan_timeout_ms before moving on. resolved_transport is
    one of lan, iroh-direct, iroh-relay or offline: the rung the last client
    measured, or the best rung certainly available when none has connected yet.
    """
    engine = get_engine(request)
    return build_transport_state(request, port=engine.port)


# ----------------- Relay setting -----------------

def _write_relay_url(config_path, relay_url: str) -> None:
    """Persist relay_url to the config file, keeping the rest of the file as is."""
    try:
        text = config_path.read_text(encoding="utf-8") if config_path.exists() else DEFAULT_CONFIG_TEMPLATE
    except Exception:
        text = DEFAULT_CONFIG_TEMPLATE

    escaped = relay_url.replace('"', '\\"')
    line = f'relay_url: "{escaped}"'
    if re.search(r"(?m)^relay_url:.*$", text):
        text = re.sub(r"(?m)^relay_url:.*$", line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")


@router.get("/config/relay")
def get_relay_config(request: Request):
    """Return the relay URL currently configured."""
    config = getattr(request.app.state, "config", None)
    return {
        "relay_url": config.relay_url if config is not None else "",
        "iroh_enabled": config.iroh_enabled if config is not None else False,
    }


@router.put("/config/relay")
async def set_relay_config(request: Request):
    """Save a new relay URL and apply it without restarting the hub.

    Only the iroh transport is stopped and started again with the new value.
    The hub process, the dashboard and every other transport keep running.
    """
    body = await request.json()
    try:
        relay_url = validate_relay_url(body.get("relay_url", ""))
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    config = getattr(request.app.state, "config", None)
    config_path = config.path if config is not None and config.path is not None else get_config_path()
    _write_relay_url(config_path, relay_url)
    if config is not None:
        config.relay_url = relay_url

    old_transport = getattr(request.app.state, "iroh", None)
    old_status = old_transport.status() if old_transport is not None else {"state": "disabled", "error": None}
    was_enabled = old_status.get("state") != "disabled"

    new_transport = IrohTransport(
        db=request.app.state.db,
        token_provider=lambda: request.app.state.auth_token,
        relay_url=relay_url,
        enabled=was_enabled,
        disabled_reason="" if was_enabled else (old_status.get("error") or ""),
    )
    if old_transport is not None:
        await old_transport.stop()
    request.app.state.iroh = new_transport
    new_transport.start()

    return {"success": True, "relay_url": relay_url}


@router.post("/config/relay/test")
async def test_relay_config(request: Request):
    """Report whether a relay URL is reachable, or the real error trying it.

    This checks that the relay host answers over HTTP. It does not open an
    iroh connection through it, so a pass here is not a guarantee iroh will
    reach it, only that the host resolves and responds.
    """
    body = await request.json()
    raw_url = body.get("relay_url", "")
    try:
        relay_url = validate_relay_url(raw_url)
    except ConfigError as exc:
        return {"success": False, "error": str(exc)}

    if not relay_url:
        return {"success": True, "message": "Using iroh's bundled public relays."}

    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            resp = await client.get(relay_url)
        return {"success": True, "message": f"{relay_url} responded with HTTP {resp.status_code}."}
    except httpx.HTTPError as exc:
        return {"success": False, "error": f"{relay_url} did not respond: {exc}"}
