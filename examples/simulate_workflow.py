"""End-to-End simulation of multi-agent collaboration with AgentRelay.

Scenario:
1. Job initialized: Task A (Codex) -> Task B (AntiGravity) -> Task C (Claude Code).
2. Codex claims and completes Task A.
3. AntiGravity waits on Task B, unblocks, and completes Task B.
4. Claude Code waits on Task C, unblocks once Task A & B finish.
5. Claude Code discovers a defect in Task A and calls request_revision!
6. Task C is re-blocked, Task A becomes 'revision_requested'.
7. Codex picks up the revision, fixes the defect, and completes Task A again.
8. Claude Code unblocks, completes Task C.
9. Whole pipeline successfully completes!
"""

import time
import yaml
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

from agent_relay.core.engine import RelayEngine
from agent_relay.core.db import Database
from agent_relay.core.models import CreateJobRequest

console = Console()


def run_simulation():
    # Use isolated test DB for demonstration
    db_path = str(Path(__file__).parent / "simulation_relay.db")
    if Path(db_path).exists():
        Path(db_path).unlink()

    engine = RelayEngine(Database(db_path))

    console.print(Panel("[bold magenta]=== Starting Multi-Agent Pipeline Simulation ===[/bold magenta]", border_style="magenta"))

    # Step 1: Load and create job
    yaml_file = Path(__file__).parent / "trio_pipeline.yaml"
    data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    job = engine.create_job(CreateJobRequest(**data))
    console.print(f"[bold green]1. Job initialized:[/bold green] '{job.id}' with {len(job.tasks)} tasks.")

    # Check initial statuses
    task_a = engine.get_task("task-A-backend-api")
    task_b = engine.get_task("task-B-db-schema")
    task_c = engine.get_task("task-C-frontend-integration")
    console.print(f"   - Task A status: [bold yellow]{task_a.status.value}[/bold yellow] (Initial, no deps)")
    console.print(f"   - Task B status: [dim]{task_b.status.value}[/dim] (Waiting on Task A)")
    console.print(f"   - Task C status: [dim]{task_c.status.value}[/dim] (Waiting on Task A & Task B)")

    # Step 2: Agent Codex works on Task A
    console.print("\n[bold cyan]2. Agent Codex claims and works on Task A...[/bold cyan]")
    engine.claim_task("task-A-backend-api", agent="codex")
    time.sleep(0.5)
    engine.complete_task(
        task_id="task-A-backend-api",
        summary="Generated FastAPI endpoints: POST /api/login, POST /api/register with JWT token creation.",
        artifacts=["backend/routes/auth.py", "backend/tests/test_auth.py"],
        agent="codex"
    )
    console.print("[bold green]   [OK] Task A completed by Codex![/bold green]")

    # Verify Task B is unlocked and ready for AntiGravity
    status_b = engine.get_task_wait_status("task-B-db-schema")
    console.print(f"   - Task B readiness: [bold yellow]Ready = {status_b.ready}[/bold yellow], Status = {status_b.status.value}")

    # Step 3: Agent AntiGravity works on Task B
    console.print("\n[bold cyan]3. Agent AntiGravity claims and works on Task B...[/bold cyan]")
    engine.claim_task("task-B-db-schema", agent="antigravity")
    time.sleep(0.5)
    engine.complete_task(
        task_id="task-B-db-schema",
        summary="Created SQLite user table with hashed passwords and Alembic migration version 001_auth.py.",
        artifacts=["backend/models/user.py", "migrations/versions/001_auth.py"],
        agent="antigravity"
    )
    console.print("[bold green]   [OK] Task B completed by AntiGravity![/bold green]")

    # Verify Task C is unlocked for Claude Code
    status_c = engine.get_task_wait_status("task-C-frontend-integration")
    console.print(f"   - Task C readiness: [bold yellow]Ready = {status_c.ready}[/bold yellow] (All upstream dependencies A & B are done)")
    console.print(f"   - Upstream outputs received: {list(status_c.upstream_summaries.keys())}")

    # Step 4: Claude Code starts Task C, but discovers a bug in Task A!
    console.print("\n[bold cyan]4. Claude Code claims Task C and begins integration tests...[/bold cyan]")
    engine.claim_task("task-C-frontend-integration", agent="claude_code")
    time.sleep(0.5)

    console.print("[bold red]   ! Claude Code finds a defect in Task A: Email case sensitivity causes 500 error on login.[/bold red]")
    console.print("[bold red]   ! Claude Code issues revision request on Task A (Codex)...[/bold red]")
    engine.request_revision(
        target_task_id="task-A-backend-api",
        feedback="In auth.py line 42: user lookup fails with 500 when email has uppercase letters. Please normalize email to lowercase before querying database.",
        from_agent="claude_code"
    )

    # Check statuses after revision requested
    task_a_rev = engine.get_task("task-A-backend-api")
    task_c_rev = engine.get_task("task-C-frontend-integration")
    job_rev = engine.get_job(job.id)

    console.print(f"   - Task A status now: [bold red]{task_a_rev.status.value}[/bold red]")
    console.print(f"   - Task C status now: [bold yellow]{task_c_rev.status.value}[/bold yellow] (Blocked pending Task A fix)")
    console.print(f"   - Job status: [bold yellow]{job_rev.status.value}[/bold yellow]")

    # Step 5: Codex picks up revision, fixes it, and completes Task A again
    console.print("\n[bold cyan]5. Codex checks its tasks, sees revision request from Claude Code...[/bold cyan]")
    codex_tasks = engine.db.list_tasks_by_agent("codex")
    for ct in codex_tasks:
        for rev in ct.get("revisions", []):
            if rev["status"] == "open":
                console.print(f"   - Defect notice received: '{rev['feedback']}'")

    console.print("   - Codex claims revision fix and updates auth.py with email normalization...")
    engine.claim_task("task-A-backend-api", agent="codex")
    time.sleep(0.5)
    engine.complete_task(
        task_id="task-A-backend-api",
        summary="Fixed email normalization in auth.py (applied .lower() on input email) and added test_login_case_insensitivity test.",
        artifacts=["backend/routes/auth.py", "backend/tests/test_auth.py"],
        agent="codex"
    )
    console.print("[bold green]   [OK] Task A revision completed by Codex![/bold green]")

    # Step 6: Claude Code is unblocked and completes Task C
    status_c_after = engine.get_task_wait_status("task-C-frontend-integration")
    console.print(f"\n[bold cyan]6. Claude Code unblocked: Ready = {status_c_after.ready}, Status = {status_c_after.status.value}[/bold cyan]")
    console.print("   - Claude Code re-runs full integration tests. All pass!")
    engine.claim_task("task-C-frontend-integration", agent="claude_code")
    engine.complete_task(
        task_id="task-C-frontend-integration",
        summary="Completed frontend auth forms and validated end-to-end flow against normalized API. All 14 tests passing.",
        artifacts=["frontend/src/AuthForm.tsx", "cypress/e2e/auth.cy.ts"],
        agent="claude_code"
    )
    console.print("[bold green]   [OK] Task C completed by Claude Code![/bold green]")

    # Step 7: Final job status
    final_job = engine.get_job(job.id)
    console.print(f"\n[bold green]7. Final Pipeline Status: {final_job.status.value.upper()}[/bold green]")
    for tid, t in final_job.tasks.items():
        console.print(f"   - {tid} ({t.assigned_agent}): {t.status.value}")

    console.print(Panel("[bold green]Simulation Completed Successfully! All agent handoffs and revision loops verified.[/bold green]", border_style="green"))


if __name__ == "__main__":
    run_simulation()
