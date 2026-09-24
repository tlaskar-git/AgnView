import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from .routes import router as api_router
from ..core.engine import RelayEngine
from ..core.db import Database
from ..core.config import load_config
from ..core.iroh_transport import IrohTransport

logger = logging.getLogger(__name__)


# Set AGNVIEW_IROH=0 to keep the hub LAN only. The default is on, because the
# whole point of the iroh transport is that the user never sets anything up.
IROH_ENV = "AGNVIEW_IROH"


def _iroh_enabled_from_env() -> bool:
    value = os.environ.get(IROH_ENV)
    if value is None:
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def create_app(db_path: Optional[str] = None, auth_token: Optional[str] = None, port: int = 8765) -> FastAPI:
    token = auth_token or os.environ.get("AGENT_RELAY_TOKEN")

    app = FastAPI(
        title="AgnView API",
        description="Cross-Agent Coordination Hub & Live Console for Claude Code, Codex, AntiGravity, and Web LLMs",
        version="0.1.7"
    )

    # Enable CORS for external tools, mobile apps, and browser extensions
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Token Authentication Middleware for distributed multi-computer and mobile setups
    if token:
        @app.middleware("http")
        async def verify_token_middleware(request: Request, call_next):
            # Only authenticate API endpoints; allow static dashboard index and docs
            path = request.url.path
            if path.startswith("/api") and not path.startswith("/api/mobile/pairing"):
                client_ip = request.client.host if request.client else "127.0.0.1"
                # Allow unauthenticated access only for local browser same-origin sessions (not programmatic API clients/tests)
                is_local_browser = (
                    client_ip in ("127.0.0.1", "::1", "localhost")
                    and (
                        request.headers.get("Sec-Fetch-Site") == "same-origin"
                        or request.headers.get("Referer", "").startswith(("http://localhost:", "http://127.0.0.1:", "http://[::1]:"))
                    )
                )
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
                    if not provided or provided != expected:
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

    iroh_transport = IrohTransport(
        db=db,
        token_provider=lambda: app.state.auth_token,
        relay_url=config.relay_url,
        enabled=iroh_enabled,
        disabled_reason=disabled_reason,
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

    @app.on_event("shutdown")
    async def _stop_iroh_transport():
        await iroh_transport.stop()

    @app.on_event("shutdown")
    async def _stop_live_agent_sessions():
        # Every CLI process AgnView holds open is a child of this one. Close
        # them here so stopping the hub never leaves an agent running.
        await engine.runner.shutdown_live_sessions()

    app.include_router(api_router)

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
