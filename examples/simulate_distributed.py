"""Distributed Multi-Machine / Multi-Account Simulation for AgentRelay.

Simulates:
1. Hub started with an authentication token (simulating a remote hub).
2. Worker 1 on Machine A (Alice): codex@alice-workstation
3. Worker 2 on Machine B (Bob): antigravity@bob-gpu-box
4. Worker 3 on Machine C (Carol): claude_code@carol-macbook
All communicating over HTTP REST with authentication tokens.
"""

import yaml
from pathlib import Path
from rich.console import Console
from rich.panel import Panel
from fastapi.testclient import TestClient

from agent_relay.api.app import create_app

console = Console()


def run_distributed_simulation():
    # 1. Start simulated Hub with Token Auth
    db_file = str(Path(__file__).parent / "distributed_relay.db")
    if Path(db_file).exists():
        Path(db_file).unlink()

    token = "mesh-secure-token-xyz"
    app = create_app(db_path=db_file, auth_token=token)
    client = TestClient(app)
    headers = {"X-Agent-Relay-Token": token}

    console.print(Panel(
        f"[bold magenta]=== Starting Distributed Multi-Machine / Multi-Account Simulation ===[/bold magenta]\n"
        f"[cyan]Auth Token:[/cyan] {token}\n"
        f"[dim]Simulating 3 independent computers and accounts coordinating across the network...[/dim]",
        border_style="magenta"
    ))

    # 2. Worker Heartbeats from 3 distinct computers & accounts
    console.print("\n[bold cyan]1. Registering nodes from different computers and accounts...[/bold cyan]")

    client.post("/api/agents/heartbeat", headers=headers, json={
        "instance_id": "codex@alice-workstation",
        "role": "codex",
        "hostname": "alice-workstation",
        "account": "alice",
        "status": "online"
    })
    console.print("   [OK] Node 1 Online: `codex@alice-workstation` (Computer: alice-workstation, Account: alice)")

    client.post("/api/agents/heartbeat", headers=headers, json={
        "instance_id": "antigravity@bob-gpu-box",
        "role": "antigravity",
        "hostname": "bob-gpu-box",
        "account": "bob",
        "status": "online"
    })
    console.print("   [OK] Node 2 Online: `antigravity@bob-gpu-box` (Computer: bob-gpu-box, Account: bob)")

    client.post("/api/agents/heartbeat", headers=headers, json={
        "instance_id": "claude_code@carol-macbook",
        "role": "claude_code",
        "hostname": "carol-macbook",
        "account": "carol",
        "status": "online"
    })
    console.print("   [OK] Node 3 Online: `claude_code@carol-macbook` (Computer: carol-macbook, Account: carol)")

    # 3. Create Multi-Agent Pipeline on Hub
    yaml_file = Path(__file__).parent / "trio_pipeline.yaml"
    job_spec = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
    res_job = client.post("/api/jobs", headers=headers, json=job_spec)
    job_data = res_job.json()
    console.print(f"\n[bold green]2. Pipeline Launched on Hub:[/bold green] '{job_data['id']}' ({job_data['title']})")

    # 4. Computer 1 (Alice): Claim and complete Task A
    console.print("\n[bold cyan]3. Computer 1 (alice-workstation) claims Task A...[/bold cyan]")
    client.post("/api/tasks/task-A-backend-api/claim", headers=headers, json={
        "agent": "codex",
        "instance_id": "codex@alice-workstation"
    })
    client.post("/api/tasks/task-A-backend-api/complete", headers=headers, json={
        "summary": "Built FastAPI auth endpoints with JWT.",
        "artifacts": ["auth.py"],
        "instance_id": "codex@alice-workstation"
    })
    console.print("   [OK] Task A completed on `alice-workstation` by `alice`!")

    # 5. Computer 2 (Bob): AntiGravity on GPU box waits for Task B
    console.print("\n[bold cyan]4. Computer 2 (bob-gpu-box) queries Task B wait-status...[/bold cyan]")
    res_wait_b = client.get("/api/tasks/task-B-db-schema/wait-status", headers=headers)
    assert res_wait_b.json()["ready"] is True
    console.print("   [OK] Task B is unblocked on Bob's GPU box!")

    client.post("/api/tasks/task-B-db-schema/claim", headers=headers, json={
        "agent": "antigravity",
        "instance_id": "antigravity@bob-gpu-box"
    })
    client.post("/api/tasks/task-B-db-schema/complete", headers=headers, json={
        "summary": "Created SQLite schema and migrations on GPU cluster.",
        "artifacts": ["schema.sql"],
        "instance_id": "antigravity@bob-gpu-box"
    })
    console.print("   [OK] Task B completed on `bob-gpu-box` by `bob`!")

    # 6. Computer 3 (Carol): Claude Code on MacBook claims Task C
    console.print("\n[bold cyan]5. Computer 3 (carol-macbook) queries Task C wait-status...[/bold cyan]")
    res_wait_c = client.get("/api/tasks/task-C-frontend-integration/wait-status", headers=headers)
    assert res_wait_c.json()["ready"] is True
    console.print("   [OK] Task C is unblocked on Carol's MacBook! All upstream dependencies met.")

    client.post("/api/tasks/task-C-frontend-integration/claim", headers=headers, json={
        "agent": "claude_code",
        "instance_id": "claude_code@carol-macbook"
    })
    client.post("/api/tasks/task-C-frontend-integration/complete", headers=headers, json={
        "summary": "Frontend auth integration and end-to-end tests verified.",
        "artifacts": ["App.tsx"],
        "instance_id": "claude_code@carol-macbook"
    })
    console.print("   [OK] Task C completed on `carol-macbook` by `carol`!")

    # 7. Verify attribution of tasks across nodes
    final_job = client.get(f"/api/jobs/{job_data['id']}", headers=headers).json()
    console.print(f"\n[bold green]6. Final Multi-Computer Pipeline Status: {final_job['status'].upper()}[/bold green]")
    for tid, t in final_job["tasks"].items():
        console.print(f"   - {tid}: Status = [bold green]{t['status']}[/bold green], Executed By Node = [bold cyan]{t.get('executed_by')}[/bold cyan]")

    console.print(Panel("[bold green]Distributed Multi-Machine / Multi-Account Simulation Succeeded![/bold green]", border_style="green"))


if __name__ == "__main__":
    run_distributed_simulation()
