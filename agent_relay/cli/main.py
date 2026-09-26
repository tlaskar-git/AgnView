"""CLI for AgentRelay: Cross-Agent Orchestration & Status Synchronization.

Supports local SQLite direct mode AND distributed multi-machine/multi-account HTTP remote mode.
"""

import os
import sys
import json
import time
import socket
import getpass
import argparse
import httpx
import yaml
from pathlib import Path
from typing import Optional, List, Dict, Any

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from .. import __version__
from ..core.engine import RelayEngine, NotFoundError, InvalidStateError
from ..core.db import Database, DEFAULT_DB_PATH
from ..core.models import (
    CreateJobRequest, UsageAccount
)
from ..core.prompts import format_web_prompt_for_agent
from ..core.network import get_network_endpoints
from ..core.pairing import get_or_create_pairing_token

console = Console()
DEFAULT_SERVER_URL = os.environ.get("AGENT_RELAY_URL", "http://127.0.0.1:8765")
DEFAULT_TOKEN = os.environ.get("AGENT_RELAY_TOKEN", None)


def get_local_engine() -> RelayEngine:
    return RelayEngine(Database())


def get_http_client(url: str, token: Optional[str] = None) -> httpx.Client:
    headers = {}
    if token:
        headers["X-Agent-Relay-Token"] = token
    return httpx.Client(base_url=url, headers=headers, timeout=30.0)


def is_server_alive(url: str, token: Optional[str] = None) -> bool:
    try:
        client = get_http_client(url, token)
        r = client.get("/api/jobs", timeout=1.5)
        return r.status_code in (200, 401)
    except Exception:
        return False


def shouldUseHttp(url: str, token: Optional[str] = None) -> bool:
    """Determine whether to use HTTP API or direct local SQLite."""
    if os.environ.get("AGENT_RELAY_DB_PATH"):
        return False
    # If URL is pointing to a remote host (not 127.0.0.1/localhost) or if server is running locally
    is_remote = not ("127.0.0.1" in url or "localhost" in url)
    if is_remote:
        return True
    return is_server_alive(url, token)


# ----------------- Commands -----------------

def cmd_serve(args):
    """Start AgnView server and web dashboard."""
    import uvicorn
    from ..core.network import is_allowed_address, get_tailscale_ip, resolve_bind_mode, BIND_MODE_ENV

    tailscale_ip = get_tailscale_ip()
    # Resolve host: default to 127.0.0.1 unless --listen-lan or --listen-overlay is specified or --host is passed explicitly
    if args.host:
        host = args.host
    elif getattr(args, "listen_overlay", False):
        if not tailscale_ip:
            console.print("[bold red]Error: --listen-overlay specified but no active overlay interface (100.64.0.0/10, for example Tailscale) was detected.[/bold red]")
            sys.exit(1)
        host = tailscale_ip
    elif args.listen_lan:
        host = "0.0.0.0"
    else:
        host = "127.0.0.1"

    # Verify that host is not bound to a public interface
    if host not in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
        if not is_allowed_address(host):
            console.print(f"[bold red]Error: Refusing to bind to public address '{host}'. AgnView is a local-only application.[/bold red]")
            sys.exit(1)

    # Record the resolved bind mode so the API (and the pairing modal) can name the active transport
    if getattr(args, "listen_overlay", False):
        bind_mode = "tailscale"
    elif args.listen_lan:
        bind_mode = "lan"
    else:
        bind_mode = resolve_bind_mode(host)
    os.environ[BIND_MODE_ENV] = bind_mode

    endpoints = get_network_endpoints(port=args.port)
    console.print(f"[bold cyan]Starting AgnView Server on {host}:{args.port}...[/bold cyan]")
    console.print(f"  • Localhost:   [bold green]http://127.0.0.1:{args.port}[/bold green]")
    if args.listen_lan or host == "0.0.0.0":
        console.print(f"  • Local Wi-Fi: [bold green]{endpoints.get('lan', 'N/A')}[/bold green]")
        if "tailscale" in endpoints and endpoints["tailscale"]:
            console.print(f"  • Tailscale:   [bold green]{endpoints['tailscale']}[/bold green]")
    elif getattr(args, "listen_overlay", False) or host == tailscale_ip:
        console.print(f"  • Tailscale:   [bold green]http://{tailscale_ip}:{args.port}[/bold green]")
    else:
        console.print("  • Remote/LAN access disabled (use --listen-lan or --listen-overlay to allow remote connections)")

    token = args.token or DEFAULT_TOKEN or get_or_create_pairing_token()
    os.environ["AGENT_RELAY_TOKEN"] = token
    console.print(f"  • Pairing Token: [bold yellow]{token}[/bold yellow]")

    # Every install should come back up after a reboot with no manual step,
    # and come back the way it is being served now. Registering a bare
    # `agnview serve` brought the hub back on loopback and the default port,
    # so a phone paired to the LAN address lost it after every reboot.
    from ..core import autostart

    autostart_args = []
    if getattr(args, "listen_overlay", False):
        autostart_args.append("--listen-overlay")
    elif args.listen_lan:
        autostart_args.append("--listen-lan")
    if args.host:
        autostart_args += ["--host", args.host]
    if args.port != 8765:
        autostart_args += ["--port", str(args.port)]
    # The token is deliberately absent: it is a secret, and the hub reads the
    # persisted one at every start anyway.

    registration = autostart.ensure_enabled_by_default(autostart_args)
    if registration == "enabled":
        console.print("  • Autostart: [bold green]enabled[/bold green] (AgnView will start automatically at login; disable with `agnview autostart disable`)")
    elif registration == "updated":
        console.print("  • Autostart: [bold green]updated[/bold green] (login will now start AgnView with these options)")

    # A bad setting stops the iroh transport and nothing else. Say so here
    # rather than letting the hub look like it came up clean.
    from ..core.config import load_config

    hub_config = load_config()
    if not hub_config.is_valid:
        console.print("[bold red]Configuration error. The iroh transport will not start:[/bold red]")
        for problem in hub_config.errors:
            console.print(f"[bold red]  • {problem}[/bold red]")
        console.print("[bold red]  The rest of the hub starts as normal.[/bold red]")
    elif hub_config.relay_url:
        console.print(f"  • iroh relay: [bold green]{hub_config.relay_url}[/bold green] (overrides the bundled relays)")

    console.print(f"[dim]Database path: {DEFAULT_DB_PATH}[/dim]\n")
    # Enable dual-stack IPv4 and IPv6 so both localhost (::1) and 127.0.0.1 work seamlessly on Windows and macOS
    if host == "0.0.0.0":
        try:
            import socket
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.bind(('::', args.port))
            sock.listen(128)
            from agent_relay.api.app import create_app
            app = create_app(port=args.port)
            config = uvicorn.Config(app, log_level="info")
            server = uvicorn.Server(config)
            server.run(sockets=[sock])
            return
        except Exception as e:
            console.print(f"[dim yellow]Dual-stack socket binding fallback ({e}), using standard binding...[/dim yellow]")

    from agent_relay.api.app import create_app
    app = create_app(port=args.port)
    uvicorn.run(app, host=host, port=args.port)


def cmd_create_job(args):
    """Create a new job from a YAML or JSON file."""
    file_path = Path(args.file)
    if not file_path.exists():
        console.print(f"[bold red]Error: File '{file_path}' does not exist.[/bold red]")
        sys.exit(1)

    content = file_path.read_text(encoding="utf-8")
    data = yaml.safe_load(content) if file_path.suffix in (".yaml", ".yml") else json.loads(content)
    req = CreateJobRequest(**data)

    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        try:
            r = client.post("/api/jobs", json=req.model_dump())
            if r.status_code != 200:
                console.print(f"[bold red]API Error ({r.status_code}): {r.text}[/bold red]")
                sys.exit(1)
            job_data = r.json()
        except Exception as e:
            console.print(f"[bold red]Failed to communicate with AgentRelay hub: {e}[/bold red]")
            sys.exit(1)
    else:
        engine = get_local_engine()
        job = engine.create_job(req)
        job_data = job.model_dump()

    console.print(Panel(
        f"[bold green]Job Created Successfully![/bold green]\n"
        f"[cyan]ID:[/cyan] {job_data['id']}\n"
        f"[cyan]Title:[/cyan] {job_data['title']}\n"
        f"[cyan]Tasks:[/cyan] {len(job_data['tasks'])} tasks registered\n"
        f"[dim]Run 'agent-relay status {job_data['id']}' to track progress.[/dim]",
        title="AgentRelay Hub",
        border_style="green"
    ))


def cmd_status(args):
    """Display status of a specific job or all jobs."""
    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        if args.job_id:
            r = client.get(f"/api/jobs/{args.job_id}")
            if r.status_code == 404:
                console.print(f"[bold red]Job '{args.job_id}' not found.[/bold red]")
                sys.exit(1)
            elif r.status_code != 200:
                console.print(f"[bold red]API Error ({r.status_code}): {r.text}[/bold red]")
                sys.exit(1)
            _render_job_data(r.json())
        else:
            r = client.get("/api/jobs")
            if r.status_code != 200:
                console.print(f"[bold red]API Error ({r.status_code}): {r.text}[/bold red]")
                sys.exit(1)
            _render_jobs_list(r.json())
    else:
        engine = get_local_engine()
        if args.job_id:
            try:
                job = engine.get_job(args.job_id)
                _render_job_data(job.model_dump())
            except NotFoundError:
                console.print(f"[bold red]Job '{args.job_id}' not found.[/bold red]")
                sys.exit(1)
        else:
            jobs = [j.model_dump() for j in engine.list_jobs()]
            _render_jobs_list(jobs)


def _render_jobs_list(jobs: List[Dict[str, Any]]):
    if not jobs:
        console.print("[yellow]No active jobs found. Create one with 'agent-relay create-job <file.yaml>'.[/yellow]")
        return

    table = Table(title="AgentRelay - Active Jobs", show_header=True, header_style="bold magenta")
    table.add_column("Job ID", style="cyan")
    table.add_column("Title", style="bold")
    table.add_column("Status")
    table.add_column("Tasks (Ready / Done / Total)")
    table.add_column("Updated At", style="dim")

    for j in jobs:
        tasks = j.get("tasks", {})
        total_tasks = len(tasks)
        done_tasks = sum(1 for t in tasks.values() if t.get("status") == "completed")
        ready_tasks = sum(1 for t in tasks.values() if t.get("status") == "ready")
        status_val = j.get("status", "pending")
        status_style = _get_status_style(status_val)

        table.add_row(
            j["id"],
            j["title"],
            Text(status_val.upper(), style=status_style),
            f"{ready_tasks} ready / {done_tasks} done / {total_tasks} total",
            j["updated_at"][:19].replace("T", " ")
        )
    console.print(table)


def _render_job_data(job_dict: Dict[str, Any]):
    status_val = job_dict.get("status", "pending")
    job_panel = (
        f"[bold cyan]Job ID:[/bold cyan] {job_dict['id']}\n"
        f"[bold]Title:[/bold] {job_dict['title']}\n"
        f"[bold]Status:[/bold] [{_get_status_style(status_val)}]{status_val.upper()}[/]\n"
        f"[dim]{job_dict.get('description') or 'No description provided'}[/dim]"
    )
    console.print(Panel(job_panel, title="Job Overview", border_style="cyan"))

    table = Table(show_header=True, header_style="bold blue")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title")
    table.add_column("Assigned Agent", style="magenta")
    table.add_column("Executed By / Node", style="green")
    table.add_column("Dependencies", style="dim")
    table.add_column("Status")
    table.add_column("Revisions", style="red")

    tasks = job_dict.get("tasks", {})
    for tid, t in tasks.items():
        status_style = _get_status_style(t.get("status", "pending"))
        deps_str = ", ".join(t.get("dependencies", [])) if t.get("dependencies") else "None (Initial)"
        open_revs = sum(1 for r in t.get("revisions", []) if r.get("status") == "open")
        rev_str = f"[bold red]! {open_revs} open[/bold red]" if open_revs > 0 else "0"
        exec_str = t.get("executed_by") or "-"

        table.add_row(
            tid,
            t.get("title", ""),
            t.get("assigned_agent", ""),
            exec_str,
            deps_str,
            Text(t.get("status", "pending").upper(), style=status_style),
            rev_str
        )
    console.print(table)

    # Revisions notice
    has_rev = False
    for t in tasks.values():
        for r in t.get("revisions", []):
            if r.get("status") == "open":
                if not has_rev:
                    console.print("\n[bold red]--- Pending Revisions & Defect Reports ---[/bold red]")
                    has_rev = True
                console.print(Panel(
                    f"[bold yellow]Task:[/bold yellow] `{t['id']}` ({t.get('title')})\n"
                    f"[bold]Assigned to:[/bold] `{t.get('assigned_agent')}`\n"
                    f"[bold]Reported by:[/bold] `{r.get('from_agent')}`\n"
                    f"[bold red]Feedback:[/bold red] {r.get('feedback')}",
                    border_style="red"
                ))


def _get_status_style(status_val: str) -> str:
    mapping = {
        "pending": "dim white",
        "ready": "bold yellow",
        "in_progress": "bold blue",
        "completed": "bold green",
        "revision_requested": "bold red",
        "failed": "bold red",
        "revision_in_progress": "bold yellow"
    }
    return mapping.get(status_val, "white")


def cmd_wait(args):
    """Block until a task's dependencies are satisfied and task is ready to start."""
    task_id = args.task_id
    timeout = args.timeout
    start_time = time.time()
    poll_interval = 2.0

    console.print(f"[bold cyan]Waiting for task '{task_id}' dependencies to complete...[/bold cyan]")

    use_http = shouldUseHttp(args.url, args.token)
    client = get_http_client(args.url, args.token) if use_http else None
    engine = None if use_http else get_local_engine()

    try:
        with console.status(f"[yellow]Checking dependencies for '{task_id}'...", spinner="dots") as spinner:
            while True:
                if use_http:
                    r = client.get(f"/api/tasks/{task_id}/wait-status")
                    if r.status_code == 404:
                        spinner.stop()
                        console.print(f"[bold red]Task '{task_id}' not found on hub.[/bold red]")
                        sys.exit(1)
                    elif r.status_code != 200:
                        spinner.stop()
                        console.print(f"[bold red]API Error: {r.text}[/bold red]")
                        sys.exit(1)
                    res_data = r.json()
                else:
                    status_res = engine.get_task_wait_status(task_id)
                    res_data = status_res.model_dump()

                if res_data["ready"]:
                    spinner.stop()
                    console.print(f"[bold green][OK] Task '{task_id}' is READY to begin![/bold green]")
                    break

                if res_data["status"] == "completed":
                    spinner.stop()
                    console.print(f"[bold green]Task '{task_id}' is ALREADY COMPLETED.[/bold green]")
                    return

                unmet_str = ", ".join(res_data["unmet_dependencies"])
                spinner.update(f"[yellow]Waiting for upstream tasks to finish: [{unmet_str}] ({int(time.time() - start_time)}s elapsed)...")

                if (time.time() - start_time) > timeout:
                    spinner.stop()
                    console.print(f"[bold red]Timeout exceeded ({timeout}s) waiting for task '{task_id}'.[/bold red]")
                    sys.exit(1)

                time.sleep(poll_interval)

        # Print upstream context
        upstream = res_data.get("upstream_summaries", {})
        if upstream:
            console.print("\n[bold]=== Upstream Agent Outputs ===[/bold]")
            for dep_id, dep_info in upstream.items():
                console.print(Panel(
                    f"[bold cyan]Task:[/bold cyan] {dep_id} ({dep_info.get('title', '')})\n"
                    f"[bold magenta]Agent:[/bold magenta] {dep_info.get('agent', '')}\n"
                    f"[bold]Status:[/bold] {dep_info.get('status', '')}\n"
                    f"[bold]Summary:[/bold]\n{dep_info.get('output_summary', 'None')}\n"
                    f"[bold]Artifacts:[/bold] {', '.join(dep_info.get('artifacts', [])) or 'None'}",
                    border_style="cyan"
                ))

        open_revs = res_data.get("open_revisions", [])
        if open_revs:
            console.print("\n[bold red]=== Open Revisions on This Task ===[/bold red]")
            for rev in open_revs:
                console.print(Panel(
                    f"[bold]From Agent:[/bold] `{rev.get('from_agent')}`\n"
                    f"[bold red]Feedback:[/bold red] {rev.get('feedback')}",
                    border_style="red"
                ))

    except NotFoundError:
        console.print(f"[bold red]Task '{task_id}' not found.[/bold red]")
        sys.exit(1)


def cmd_claim(args):
    """Claim a task and mark it in_progress."""
    instance_id = args.instance_id or f"{args.agent}@{socket.gethostname()}-{getpass.getuser()}"

    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        r = client.post(f"/api/tasks/{args.task_id}/claim", json={
            "agent": args.agent,
            "instance_id": instance_id
        })
        if r.status_code != 200:
            console.print(f"[bold red]Error claiming task ({r.status_code}): {r.text}[/bold red]")
            sys.exit(1)
        console.print(f"[bold green][OK] Task '{args.task_id}' claimed by '{instance_id}' (Status: IN_PROGRESS)[/bold green]")
    else:
        engine = get_local_engine()
        try:
            task = engine.claim_task(args.task_id, args.agent, instance_id)
            console.print(f"[bold green][OK] Task '{args.task_id}' claimed by '{task.executed_by or task.assigned_agent}' (Status: IN_PROGRESS)[/bold green]")
        except (NotFoundError, InvalidStateError) as e:
            console.print(f"[bold red]Error: {e}[/bold red]")
            sys.exit(1)


def cmd_complete(args):
    """Mark a task completed, unblocking downstream dependencies."""
    instance_id = args.instance_id or f"{args.agent or 'agent'}@{socket.gethostname()}-{getpass.getuser()}"
    artifacts = args.artifact or []

    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        r = client.post(f"/api/tasks/{args.task_id}/complete", json={
            "summary": args.summary,
            "artifacts": artifacts,
            "agent": args.agent,
            "instance_id": instance_id
        })
        if r.status_code != 200:
            console.print(f"[bold red]Error completing task ({r.status_code}): {r.text}[/bold red]")
            sys.exit(1)
        task_data = r.json()
    else:
        engine = get_local_engine()
        try:
            task = engine.complete_task(
                task_id=args.task_id,
                summary=args.summary,
                artifacts=artifacts,
                agent=args.agent,
                instance_id=instance_id
            )
            task_data = task.model_dump()
        except NotFoundError as e:
            console.print(f"[bold red]Error: {e}[/bold red]")
            sys.exit(1)

    console.print(Panel(
        f"[bold green][OK] Task '{task_data['id']}' Marked COMPLETED![/bold green]\n"
        f"[bold]Assigned:[/bold] {task_data['assigned_agent']}\n"
        f"[bold]Executed By:[/bold] {task_data.get('executed_by') or instance_id}\n"
        f"[bold]Summary:[/bold] {task_data['output_summary']}\n"
        f"[bold]Artifacts:[/bold] {', '.join(task_data['artifacts']) or 'None'}\n\n"
        f"[cyan]Downstream dependent tasks have been evaluated and unlocked if prerequisites are met.[/cyan]",
        title="Task Completed",
        border_style="green"
    ))


def cmd_reject(args):
    """Request a revision on an upstream task."""
    from_agent = args.by or f"{getpass.getuser()}@{socket.gethostname()}"

    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        r = client.post(f"/api/tasks/{args.task_id}/request-revision", json={
            "feedback": args.feedback,
            "from_agent": from_agent
        })
        if r.status_code != 200:
            console.print(f"[bold red]Error requesting revision ({r.status_code}): {r.text}[/bold red]")
            sys.exit(1)
        rev_data = r.json()
    else:
        engine = get_local_engine()
        try:
            rev = engine.request_revision(
                target_task_id=args.task_id,
                feedback=args.feedback,
                from_agent=from_agent
            )
            rev_data = rev.model_dump()
        except NotFoundError as e:
            console.print(f"[bold red]Error: {e}[/bold red]")
            sys.exit(1)

    console.print(Panel(
        f"[bold red]! Revision Requested for Task '{rev_data['task_id']}'[/bold red]\n"
        f"[bold]Sent To Agent:[/bold] `{rev_data['target_agent']}`\n"
        f"[bold]Reported By:[/bold] `{rev_data['from_agent']}`\n"
        f"[bold]Feedback / Required Fixes:[/bold]\n{rev_data['feedback']}\n\n"
        f"[yellow]Task status set to REVISION_REQUESTED. Downstream tasks are temporarily held pending the fix.[/yellow]",
        title="Revision Requested",
        border_style="red"
    ))


def cmd_my_tasks(args):
    """List tasks assigned to a specific agent role or instance."""
    agent_query = args.agent
    engine = get_local_engine()

    # Query tasks
    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        r = client.get("/api/jobs")
        all_jobs = r.json() if r.status_code == 200 else []
        tasks_data = []
        for j in all_jobs:
            for t in j.get("tasks", {}).values():
                assigned = t.get("assigned_agent", "")
                if assigned == agent_query or assigned.startswith(f"{agent_query}@") or agent_query in assigned:
                    tasks_data.append(t)
    else:
        all_tasks = engine.db.list_tasks_by_agent(agent_query)
        # Also check all tasks if role prefix matches
        tasks_data = all_tasks

    if not tasks_data:
        console.print(f"[yellow]No tasks found assigned to agent '{agent_query}'.[/yellow]")
        return

    table = Table(title=f"Tasks for Agent: {agent_query}", show_header=True, header_style="bold magenta")
    table.add_column("Task ID", style="cyan")
    table.add_column("Job ID", style="dim")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Executed By", style="green")
    table.add_column("Revisions", style="red")

    for td in tasks_data:
        status_style = _get_status_style(td["status"])
        revs = td.get("revisions", [])
        open_revs = sum(1 for r in revs if r.get("status") == "open")
        rev_str = f"[bold red]! {open_revs} open[/bold red]" if open_revs > 0 else "0"

        table.add_row(
            td["id"],
            td.get("job_id", ""),
            td["title"],
            Text(td["status"].upper(), style=status_style),
            td.get("executed_by") or "-",
            rev_str
        )
    console.print(table)


def cmd_nodes(args):
    """List connected agent instances, computers, and accounts."""
    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        r = client.get("/api/agents?nodes=true")
        if r.status_code != 200:
            console.print(f"[bold red]Error querying agents ({r.status_code}): {r.text}[/bold red]")
            sys.exit(1)
        agents = r.json()
    else:
        engine = get_local_engine()
        agents = [a.model_dump() for a in engine.list_agents()]

    if not agents:
        console.print("[yellow]No connected nodes or agent instances registered yet.[/yellow]")
        console.print("[dim]Run 'agent-relay worker' on other machines/accounts to register.[/dim]")
        return

    table = Table(title="Connected Agent Instances & Nodes", show_header=True, header_style="bold cyan")
    table.add_column("Instance ID", style="bold cyan")
    table.add_column("Role", style="magenta")
    table.add_column("Hostname / Machine")
    table.add_column("Account", style="dim")
    table.add_column("Status")
    table.add_column("Current Task", style="yellow")
    table.add_column("Last Heartbeat", style="dim")

    for a in agents:
        status = a.get("status", "offline")
        status_style = "bold green" if status == "online" else ("bold blue" if status == "busy" else "dim red")
        table.add_row(
            a["instance_id"],
            a["role"],
            a.get("hostname", ""),
            a.get("account", ""),
            Text(status.upper(), style=status_style),
            a.get("current_task_id") or "-",
            a.get("last_heartbeat", "")[:19].replace("T", " ")
        )
    console.print(table)


def cmd_worker(args):
    """
    Run continuous worker daemon on this computer/account.
    Registers heartbeat to hub and polls for ready tasks assigned to this role.
    """
    hostname = args.name or socket.gethostname()
    account = args.account or getpass.getuser()
    role = args.role
    instance_id = f"{role}@{hostname}-{account}"
    poll_interval = args.poll

    console.print(Panel(
        f"[bold green]Starting AgentRelay Worker Daemon[/bold green]\n"
        f"[cyan]Instance ID:[/cyan] {instance_id}\n"
        f"[cyan]Role:[/cyan] {role}\n"
        f"[cyan]Machine / Host:[/cyan] {hostname}\n"
        f"[cyan]Account:[/cyan] {account}\n"
        f"[cyan]Hub URL:[/cyan] {args.url}\n"
        f"[dim]Sending heartbeats every {poll_interval}s. Watching for assigned tasks...[/dim]",
        title="Worker Daemon Active",
        border_style="green"
    ))

    client = get_http_client(args.url, args.token)

    while True:
        try:
            # 1. Send heartbeat
            hb_payload = {
                "instance_id": instance_id,
                "role": role,
                "hostname": hostname,
                "account": account,
                "status": "online"
            }
            r_hb = client.post("/api/agents/heartbeat", json=hb_payload)
            if r_hb.status_code == 401:
                console.print("[bold red]Authentication Error: Invalid or missing token for hub.[/bold red]")
                sys.exit(1)

            # 2. Check for ready tasks
            r_jobs = client.get("/api/jobs")
            if r_jobs.status_code == 200:
                jobs = r_jobs.json()
                for j in jobs:
                    for tid, t in j.get("tasks", {}).items():
                        # Check if task is READY or REVISION_REQUESTED and assigned to our role
                        assigned = t.get("assigned_agent", "")
                        is_my_task = (assigned == role or assigned == instance_id or assigned.startswith(f"{role}@"))
                        if is_my_task and t.get("status") in ("ready", "revision_requested"):
                            console.print(Panel(
                                f"[bold yellow]🔔 TASK READY FOR EXECUTION![/bold yellow]\n"
                                f"[bold]Task:[/bold] {tid} ({t.get('title')})\n"
                                f"[bold]Job:[/bold] {j['id']} ({j.get('title')})\n"
                                f"[bold]Status:[/bold] {t.get('status')}\n"
                                f"[dim]{t.get('description')}[/dim]\n\n"
                                f"[cyan]To claim and work on this task:[/cyan]\n"
                                f"agent-relay claim {tid} --agent {role} --instance-id {instance_id} --url {args.url}\n"
                                f"agent-relay complete {tid} --summary \"...\" --url {args.url}",
                                border_style="yellow"
                            ))

            time.sleep(poll_interval)
        except KeyboardInterrupt:
            console.print("\n[yellow]Worker shutting down...[/yellow]")
            try:
                client.post("/api/agents/heartbeat", json={
                    "instance_id": instance_id,
                    "role": role,
                    "hostname": hostname,
                    "account": account,
                    "status": "offline"
                })
            except Exception:
                pass
            break
        except Exception as e:
            console.print(f"[dim red]Error communicating with hub ({e}), retrying in {poll_interval}s...[/dim red]")
            time.sleep(poll_interval)


def cmd_export_prompt(args):
    """Generate ready-to-paste context prompt for Claude.ai, ChatGPT.com, or Google Gemini."""
    if shouldUseHttp(args.url, args.token):
        client = get_http_client(args.url, args.token)
        r = client.get(f"/api/tasks/{args.task_id}/web-prompt?target_llm={args.target}")
        if r.status_code != 200:
            console.print(f"[bold red]API Error ({r.status_code}): {r.text}[/bold red]")
            sys.exit(1)
        prompt_text = r.json()["prompt"]
    else:
        engine = get_local_engine()
        try:
            task = engine.get_task(args.task_id)
            job = engine.get_job(task.job_id)
            prompt_text = format_web_prompt_for_agent(
                job=job,
                task=task,
                server_url=args.url,
                target_llm=args.target
            )
        except NotFoundError as e:
            console.print(f"[bold red]Error: {e}[/bold red]")
            sys.exit(1)

    console.print(f"[bold green]=== Generated Prompt for {args.target.upper()} ===[/bold green]\n")
    print(prompt_text)


def cmd_usage(args):
    """Display live subscription quotas and usage for Claude, ChatGPT, and Gemini."""
    use_http = shouldUseHttp(args.url, args.token)
    if use_http:
        client = get_http_client(args.url, args.token)
        if args.refresh:
            with console.status("[yellow]Refreshing live quotas from provider sites...", spinner="dots"):
                client.post("/api/usage/refresh-all")
        params = {"provider": args.provider} if args.provider else {}
        r = client.get("/api/usage/accounts", params=params)
        if r.status_code != 200:
            console.print(f"[bold red]Error querying usage accounts ({r.status_code}): {r.text}[/bold red]")
            sys.exit(1)
        accounts = r.json()
    else:
        from ..core.usage import UnknownProvider, fetch_observation, observation_is_stale
        engine = get_local_engine()
        raw_accounts = engine.db.list_usage_accounts(provider=args.provider)
        accounts = []
        for raw in raw_accounts:
            acc = UsageAccount(**raw)
            if args.refresh or observation_is_stale(acc.observation):
                try:
                    acc.observation = fetch_observation(acc, previous=acc.observation)
                except UnknownProvider as exc:
                    acc.observation = None
                    acc.error_message = str(exc)
                else:
                    engine.db.save_usage_account(acc.model_dump())
            accounts.append(acc.masked())

    if not accounts:
        console.print("[yellow]No subscription accounts configured yet.[/yellow]")
        console.print("[dim]Add accounts via the Web Dashboard or API (POST /api/usage/accounts).[/dim]")
        return

    table = Table(title="AI Subscription Quota & Usage Dashboard", show_header=True, header_style="bold magenta")
    table.add_column("Provider", style="bold")
    table.add_column("Account Name", style="cyan")
    table.add_column("Plan Tier", style="magenta")
    table.add_column("Usage Progress", style="bold")
    table.add_column("Used / Limit")
    table.add_column("Remaining")
    table.add_column("Reset Window", style="dim")
    table.add_column("Status")

    for a in accounts:
        provider = a.get("provider", "").upper()
        name = a.get("name", "")
        plan = a.get("plan_name", "Pro")
        pct = a.get("percent_used", 0.0)

        # Progress bar visualization: [####------] 42.0% (safe ASCII for Windows console)
        bar_len = 10
        filled = min(bar_len, max(0, int((pct / 100.0) * bar_len)))
        bar_str = "#" * filled + "-" * (bar_len - filled)

        pct_style = "bold green" if pct < 70 else ("bold yellow" if pct < 90 else "bold red")
        progress_styled = Text(f"[{bar_str}] {pct:.1f}%", style=pct_style)

        if a.get("cost_limit_usd"):
            used_str = f"${a.get('cost_used_usd', 0.0):.2f} / ${a.get('cost_limit_usd', 0.0):.2f}"
            rem_str = f"${max(0.0, a.get('cost_limit_usd', 0.0) - a.get('cost_used_usd', 0.0)):.2f}"
        else:
            used_tok = a.get("tokens_used", 0)
            lim_tok = a.get("tokens_limit", 0)
            rem_tok = a.get("tokens_remaining", 0)
            used_str = f"{used_tok:,} / {lim_tok:,} tok"
            rem_str = f"{rem_tok:,} tok"

        status = a.get("status", "active")
        status_style = "bold green" if status == "active" else ("bold yellow" if status == "warning" else "bold red")

        table.add_row(
            provider,
            name,
            plan,
            progress_styled,
            used_str,
            rem_str,
            a.get("reset_time") or "Rolling",
            Text(status.upper(), style=status_style)
        )

    console.print(table)


def cmd_autostart(args):
    """Enable, disable, or show the status of starting AgnView at login."""
    from ..core import autostart

    try:
        if args.autostart_action == "enable":
            result = autostart.enable_and_clear_opt_out()
            console.print(Panel(result, title="Autostart", style="bold green"))
        elif args.autostart_action == "disable":
            result = autostart.disable_and_remember_opt_out()
            console.print(Panel(result, title="Autostart", style="bold yellow"))
        elif args.autostart_action == "status":
            enabled = autostart.status()
            if enabled:
                console.print("[bold green]Autostart is ENABLED[/bold green]: AgnView is set to start at login.")
            else:
                console.print("[bold red]Autostart is DISABLED[/bold red]: AgnView will not start at login.")
    except RuntimeError as e:
        console.print(f"[bold red]Autostart error: {e}[/bold red]")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="agnview",
        description="AgnView: Distributed Multi-Agent & Multi-Machine Orchestration Hub"
    )
    parser.add_argument("--version", action="version", version=f"agnview {__version__}")
    parser.add_argument("--url", default=DEFAULT_SERVER_URL, help="AgentRelay server URL (default http://127.0.0.1:8765)")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Optional authentication token for multi-machine setups")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # serve
    p_serve = subparsers.add_parser("serve", help="Start the AgnView server and web UI")
    p_serve.add_argument("--host", default=None, help="Host address to bind (defaults to 127.0.0.1, or 0.0.0.0 with --listen-lan)")
    p_serve.add_argument("--port", type=int, default=8765, help="Port number (default 8765)")
    p_serve.add_argument("--listen-lan", action="store_true", help="Allow connections from local network (binds 0.0.0.0)")
    p_serve.add_argument("--listen-overlay", dest="listen_overlay", action="store_true", help="Allow connections over an overlay network such as Tailscale or NetBird (binds 100.x.y.z, inside 100.64.0.0/10)")
    # Undocumented alias kept for backward compatibility: --listen-tailscale still works,
    # it just no longer appears in --help. Both set the same listen_overlay flag.
    p_serve.add_argument("--listen-tailscale", dest="listen_overlay", action="store_true", help=argparse.SUPPRESS)
    p_serve.add_argument("--token", help="Secret token required for connecting clients")
    p_serve.set_defaults(func=cmd_serve)

    # create-job
    p_create = subparsers.add_parser("create-job", help="Create a new job from YAML/JSON file")
    p_create.add_argument("file", help="Path to pipeline.yaml or pipeline.json")
    p_create.set_defaults(func=cmd_create_job)

    # status
    p_status = subparsers.add_parser("status", help="Show job and task status")
    p_status.add_argument("job_id", nargs="?", help="Optional job ID to inspect")
    p_status.set_defaults(func=cmd_status)

    # wait
    p_wait = subparsers.add_parser("wait", help="Block until task dependencies are satisfied")
    p_wait.add_argument("task_id", help="Task ID to wait on")
    p_wait.add_argument("--timeout", type=int, default=600, help="Max wait timeout in seconds (default 600)")
    p_wait.set_defaults(func=cmd_wait)

    # claim
    p_claim = subparsers.add_parser("claim", help="Claim a task to begin work")
    p_claim.add_argument("task_id", help="Task ID to claim")
    p_claim.add_argument("--agent", required=True, help="Agent role (e.g. codex, antigravity, claude_code)")
    p_claim.add_argument("--instance-id", help="Optional specific instance identifier (e.g. codex@laptop-alice)")
    p_claim.set_defaults(func=cmd_claim)

    # complete
    p_complete = subparsers.add_parser("complete", help="Mark a task completed")
    p_complete.add_argument("task_id", help="Task ID to complete")
    p_complete.add_argument("--summary", required=True, help="Summary of work performed")
    p_complete.add_argument("--artifact", action="append", help="Artifact/file path created (can specify multiple)")
    p_complete.add_argument("--agent", help="Agent completing the task")
    p_complete.add_argument("--instance-id", help="Optional instance/machine identifier")
    p_complete.set_defaults(func=cmd_complete)

    # reject / request-revision
    p_reject = subparsers.add_parser("reject", aliases=["request-revision"], help="Request revision on an upstream task")
    p_reject.add_argument("task_id", help="Upstream task ID to send back for revision")
    p_reject.add_argument("--feedback", required=True, help="Feedback explaining defect and required fixes")
    p_reject.add_argument("--by", help="Agent or machine requesting revision")
    p_reject.set_defaults(func=cmd_reject)

    # my-tasks
    p_mytasks = subparsers.add_parser("my-tasks", help="List tasks assigned to an agent")
    p_mytasks.add_argument("--agent", required=True, help="Agent role or instance ID")
    p_mytasks.set_defaults(func=cmd_my_tasks)

    # nodes
    p_nodes = subparsers.add_parser("nodes", help="List connected agent instances and computers")
    p_nodes.set_defaults(func=cmd_nodes)

    # worker
    p_worker = subparsers.add_parser("worker", help="Run background daemon polling for assigned tasks")
    p_worker.add_argument("--role", required=True, help="Agent role to act as (e.g. claude_code, codex, antigravity)")
    p_worker.add_argument("--name", help="Machine or worker name (defaults to hostname)")
    p_worker.add_argument("--account", help="Account or user identifier (defaults to current user)")
    p_worker.add_argument("--poll", type=int, default=5, help="Poll interval in seconds (default 5)")
    p_worker.set_defaults(func=cmd_worker)

    # export-prompt
    p_prompt = subparsers.add_parser("export-prompt", help="Generate prompt for Claude.ai, ChatGPT.com, or Google Gemini")
    p_prompt.add_argument("task_id", help="Task ID to generate prompt for")
    p_prompt.add_argument("--target", default="claude.ai", choices=["claude.ai", "chatgpt", "gemini", "gemini.google.com"], help="Target LLM web interface (claude.ai, chatgpt, gemini)")
    p_prompt.set_defaults(func=cmd_export_prompt)

    # usage
    p_usage = subparsers.add_parser("usage", help="Display live subscription quotas and usage for Claude, ChatGPT, and Gemini")
    p_usage.add_argument("--provider", choices=["claude", "chatgpt", "gemini"], help="Filter by provider")
    p_usage.add_argument("--refresh", action="store_true", help="Force live fetch from provider sites")
    p_usage.set_defaults(func=cmd_usage)

    # autostart
    p_autostart = subparsers.add_parser("autostart", help="Manage starting AgnView automatically at login")
    autostart_subparsers = p_autostart.add_subparsers(dest="autostart_action", required=True)
    autostart_subparsers.add_parser("enable", help="Register AgnView to start at login")
    autostart_subparsers.add_parser("disable", help="Remove the autostart-at-login registration")
    autostart_subparsers.add_parser("status", help="Show whether autostart at login is enabled")
    p_autostart.set_defaults(func=cmd_autostart)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
