import asyncio
import logging
import os
import secrets
import uuid
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .local_only import PAIRING_PREFIX, local_browser_request, pairing_request_allowed, refuse_pairing_request
from .routes import router as api_router
from .upload_routes import router as upload_router
from ..core.engine import RelayEngine
from ..core.db import Database
from ..core.config import load_config
from ..core.iroh_transport import IrohTransport
from ..core.uploads import UploadLimits, UploadManager, resolve_uploads_dir

logger = logging.getLogger(__name__)


# Set AGNVIEW_IROH=0 to keep the hub LAN only. The default is on, because the
# whole point of the iroh transport is that the user never sets anything up.
IROH_ENV = "AGNVIEW_IROH"


# Set AGNVIEW_IROH_API=0 to serve only the live console over iroh. The mobile
# API over iroh is on whenever iroh is, gated by the same pairing key as the
# LAN. The Allow phones on my network switch does not govern it: that switch
# only picks the LAN address, and iroh never depended on it.
IROH_API_ENV = "AGNVIEW_IROH_API"


# Set AGNVIEW_UPLOADS=0 to refuse every phone upload, or AGNVIEW_IROH_UPLOADS=0
# to accept them on the LAN only. Both default to on and both need the matching
# switch in config.yaml to be on too.
UPLOADS_ENV = "AGNVIEW_UPLOADS"
IROH_UPLOADS_ENV = "AGNVIEW_IROH_UPLOADS"

# How often finished and abandoned uploads are cleared away.
UPLOAD_CLEANUP_SECONDS = 300.0


def _env_switch(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def _iroh_enabled_from_env() -> bool:
    return _env_switch(IROH_ENV)


def _iroh_api_enabled_from_env() -> bool:
    return _env_switch(IROH_API_ENV)


def create_app(db_path: Optional[str] = None, auth_token: Optional[str] = None, port: int = 8765) -> FastAPI:
    token = auth_token or os.environ.get("AGENT_RELAY_TOKEN")

    app = FastAPI(
        title="AgnView API",
        description="Cross-Agent Coordination Hub & Live Console for Claude Code, Codex, AntiGravity, and Web LLMs",
        version="0.1.14"
    )

    # No CORS. The dashboard is served by the hub itself, so its requests are
    # same-origin, and native phone apps are not bound by CORS. A wildcard would
    # let any web page the operator visits read the pairing key.

    # The pairing routes hand out the pairing key. They are open to the local
    # dashboard only, with or without an auth token, and whatever interface the
    # hub listens on.
    @app.middleware("http")
    async def pairing_routes_are_local_only(request: Request, call_next):
        if request.url.path.startswith(PAIRING_PREFIX) and not pairing_request_allowed(request, port):
            return refuse_pairing_request()
        return await call_next(request)

    # Token Authentication Middleware for distributed multi-computer and mobile setups
    if token:
        @app.middleware("http")
        async def verify_token_middleware(request: Request, call_next):
            # Only authenticate API endpoints; allow static dashboard index and docs
            path = request.url.path
            if path.startswith("/api") and not path.startswith("/api/mobile/pairing"):
                client_ip = request.client.host if request.client else "127.0.0.1"
                # Allow unauthenticated access only for the hub's own dashboard
                # on this computer: a loopback client, this hub's own Host and
                # port, and a same-origin sign. Not programmatic API clients,
                # and not a DNS-rebound page or a page on another local port.
                is_local_browser = local_browser_request(request, port)
                if not is_local_browser:
                    from ..core.pairing import check_auth_rate_limit, record_failed_auth, reset_auth_rate_limit

                    # Check rate limiting for failed auth attempts
                    if not check_auth_rate_limit(client_ip):
                        return JSONResponse(
                            status_code=429,
                            content={"detail": "Too many failed authentication attempts. Please try again later."}
                        )

                    auth_header = (
                        request.headers.get("X-AgnView-Token") or
                        request.headers.get("X-Agent-Relay-Token") or
                        request.headers.get("Authorization")
                    )
                    token_param = request.query_params.get("token")

                    provided = None
                    if auth_header:
                        if auth_header.startswith("Bearer "):
                            provided = auth_header[7:].strip()
                        else:
                            provided = auth_header.strip()
                    elif token_param:
                        provided = token_param.strip()

                    expected = request.app.state.auth_token or token
                    if not provided or not secrets.compare_digest(provided.encode("utf-8"), str(expected).encode("utf-8")):
                        record_failed_auth(client_ip)
                        return JSONResponse(
                            status_code=401,
                            content={"detail": "Unauthorized: Invalid or missing AgnView authentication token."}
                        )

                    reset_auth_rate_limit(client_ip)

            return await call_next(request)

    db = Database(db_path)
    engine = RelayEngine(db, port=port)
    app.state.db = db
    app.state.engine = engine
    app.state.auth_token = token

    config = load_config()
    app.state.config = config

    # A configuration the hub cannot act on stops iroh and nothing else. The
    # reason is already in the log and travels to the API in the status.
    if not _iroh_enabled_from_env():
        iroh_enabled, disabled_reason = False, f"{IROH_ENV} is set to off"
    elif not config.iroh_enabled:
        iroh_enabled, disabled_reason = False, f"iroh_enabled is false in {config.path}"
    elif not config.is_valid:
        iroh_enabled, disabled_reason = False, "; ".join(config.errors)
    else:
        iroh_enabled, disabled_reason = True, ""

    # Phone uploads. A configuration the hub cannot act on turns them off,
    # because a write path is never left on by guesswork.
    upload_manager = None
    upload_janitor = None
    uploads_folder = resolve_uploads_dir(config.uploads_dir, db.db_path)
    upload_limits = UploadLimits.from_config(config) if config.is_valid else UploadLimits()
    try:
        if _env_switch(UPLOADS_ENV) and config.uploads_enabled and config.is_valid:
            upload_manager = UploadManager(uploads_folder, upload_limits, in_use=db.list_task_file_paths)
        elif os.path.isdir(uploads_folder):
            # Uploads are off, but files an earlier run stored are still on
            # disk. A manager that is never reachable from a route still clears
            # away the expired ones. It creates nothing.
            upload_janitor = UploadManager(uploads_folder, upload_limits, in_use=db.list_task_file_paths)
    except OSError as exc:
        logger.warning("uploads are off: the uploads folder is not usable: %s", exc)
    upload_cleaner = upload_manager or upload_janitor
    iroh_uploads_enabled = (
        upload_manager is not None and _env_switch(IROH_UPLOADS_ENV) and config.iroh_uploads_enabled
    )
    app.state.uploads = upload_manager
    app.state.uploads_dir = str(upload_manager.root) if upload_manager is not None else None
    app.state.iroh_uploads_enabled = iroh_uploads_enabled

    iroh_transport = IrohTransport(
        db=db,
        token_provider=lambda: app.state.auth_token,
        relay_url=config.relay_url,
        enabled=iroh_enabled,
        disabled_reason=disabled_reason,
        asgi_app=app,
        api_enabled=_iroh_api_enabled_from_env() and config.iroh_api_enabled,
        uploads=upload_manager if iroh_uploads_enabled else None,
    )
    app.state.iroh = iroh_transport

    @app.on_event("startup")
    async def _bind_event_loop():
        # Job and task routes are sync, so they execute on worker threads.
        # Remember the serving loop so their broadcasts still reach SSE clients.
        engine.bind_loop(asyncio.get_running_loop())

    @app.on_event("startup")
    async def _seed_discovered_usage_accounts():
        """Give a fresh install a working Usage tab without any setup.

        Every adapter reads a tool's own local sign-in and needs no credential,
        so an empty Usage tab on a machine that has Claude Code and Codex
        signed in meant nothing but "nobody clicked Add Account yet". Each tool
        found here gets an account once; an account the operator deleted is not
        recreated, because only providers with no account at all are added and
        a deleted one is only re-seeded if the whole set is empty.
        """
        try:
            from ..core.usage.discover import missing_providers
            from ..core.models import UsageAccount

            from ..core.usage.discover import dismissed_providers

            existing = db.list_usage_accounts()
            # Every start, not only the first: a tool signed in after the first
            # start used to need Add Account by hand. A provider the operator
            # removed on purpose is skipped.
            have = [account.get("provider") for account in existing]
            for found in missing_providers(have, skip=dismissed_providers()):
                account = UsageAccount(
                    id=f"{found['provider']}-{uuid.uuid4().hex[:6]}",
                    provider=found["provider"],
                    name=found["name"],
                    auth_type="session_token",
                    credential="",
                    plan_name=found.get("plan_name") or "Unknown",
                )
                db.save_usage_account(account.model_dump())
                logger.info(
                    "Usage: added %s from %s", found["provider"], found["detected_from"]
                )
        except Exception as exc:
            # A hub that cannot seed still serves the dashboard.
            logger.warning("Usage: could not seed discovered accounts: %s", exc)

    @app.on_event("startup")
    async def _start_iroh_transport():
        # start() schedules the bind and returns. Startup never waits on the
        # network, so a hub with no route out still comes up and serves the
        # dashboard.
        iroh_transport.start()

    cleanup_tasks = []

    @app.on_event("startup")
    async def _start_upload_cleanup():
        if upload_cleaner is None:
            return

        async def loop_forever():
            while True:
                try:
                    await asyncio.to_thread(upload_cleaner.cleanup)
                except Exception as exc:
                    logger.warning("upload cleanup failed: %s", exc)
                await asyncio.sleep(UPLOAD_CLEANUP_SECONDS)

        cleanup_tasks.append(asyncio.ensure_future(loop_forever()))

    @app.on_event("shutdown")
    async def _stop_upload_cleanup():
        for task in cleanup_tasks:
            task.cancel()

    @app.on_event("shutdown")
    async def _stop_iroh_transport():
        await iroh_transport.stop()

    @app.on_event("shutdown")
    async def _stop_live_agent_sessions():
        # Every CLI process AgnView holds open is a child of this one. Close
        # them here so stopping the hub never leaves an agent running.
        await engine.runner.shutdown_live_sessions()

    app.include_router(api_router)
    app.include_router(upload_router)

    # Web Dashboard Static UI & Assets
    web_dir = Path(__file__).parent.parent / "web"
    static_dir = web_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    index_file = web_dir / "templates" / "index.html"
    if index_file.exists():
        @app.get("/", include_in_schema=False)
        async def serve_index():
            # FileResponse sets no Cache-Control of its own, so a browser is
            # free to serve this page from its heuristic cache on a plain
            # navigation, load, or F5, and did: an operator reinstalling a
            # fixed build restarted the hub, and their already-open tab kept
            # running JavaScript from hours earlier because nothing told the
            # browser this page had changed. This is the one HTML document
            # every fix in this dashboard depends on being current, so it is
            # never cached.
            return FileResponse(
                str(index_file),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

    return app
