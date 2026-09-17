"""Unit tests for AgentRelay state machine and engine."""

import pytest
from agent_relay.core.engine import RelayEngine, DependencyCycleError, InvalidStateError
from agent_relay.core.db import Database
from agent_relay.core.models import CreateJobRequest, TaskSpec, TaskStatus


@pytest.fixture
def test_engine(tmp_path):
    db_file = tmp_path / "test_relay.db"
    return RelayEngine(Database(str(db_file)))


def test_create_job_and_dependency_resolution(test_engine):
    req = CreateJobRequest(
        id="job-test-1",
        title="Test Pipeline",
        description="Testing dependencies",
        tasks=[
            TaskSpec(id="task-A", title="Task A", assigned_agent="codex", dependencies=[]),
            TaskSpec(id="task-B", title="Task B", assigned_agent="antigravity", dependencies=["task-A"]),
            TaskSpec(id="task-C", title="Task C", assigned_agent="claude_code", dependencies=["task-A", "task-B"])
        ]
    )
    job = test_engine.create_job(req)
    assert job.id == "job-test-1"
    assert len(job.tasks) == 3

    # Task A has 0 deps -> READY
    assert test_engine.get_task("task-A").status == TaskStatus.READY
    # Tasks B & C have deps -> PENDING
    assert test_engine.get_task("task-B").status == TaskStatus.PENDING
    assert test_engine.get_task("task-C").status == TaskStatus.PENDING


def test_cycle_detection(test_engine):
    req = CreateJobRequest(
        id="job-cycle",
        title="Cycle Test",
        tasks=[
            TaskSpec(id="task-1", title="Task 1", assigned_agent="codex", dependencies=["task-2"]),
            TaskSpec(id="task-2", title="Task 2", assigned_agent="claude_code", dependencies=["task-1"])
        ]
    )
    with pytest.raises(DependencyCycleError):
        test_engine.create_job(req)


def test_task_claim_and_completion_lifecycle(test_engine):
    req = CreateJobRequest(
        id="job-lifecycle",
        title="Lifecycle Test",
        tasks=[
            TaskSpec(id="task-A", title="A", assigned_agent="codex", dependencies=[]),
            TaskSpec(id="task-B", title="B", assigned_agent="antigravity", dependencies=["task-A"])
        ]
    )
    test_engine.create_job(req)

    # Claim task A
    claimed = test_engine.claim_task("task-A", agent="codex")
    assert claimed.status == TaskStatus.IN_PROGRESS

    # Cannot claim task B yet because dependencies are not done
    with pytest.raises(InvalidStateError):
        test_engine.claim_task("task-B", agent="antigravity")

    # Complete task A
    completed_a = test_engine.complete_task(
        task_id="task-A",
        summary="API built",
        artifacts=["api.py"],
        agent="codex"
    )
    assert completed_a.status == TaskStatus.COMPLETED

    # Task B should now be automatically unlocked to READY
    task_b = test_engine.get_task("task-B")
    assert task_b.status == TaskStatus.READY

    wait_b = test_engine.get_task_wait_status("task-B")
    assert wait_b.ready is True
    assert "task-A" in wait_b.upstream_summaries
    assert wait_b.upstream_summaries["task-A"]["output_summary"] == "API built"


def test_revision_feedback_loop(test_engine):
    req = CreateJobRequest(
        id="job-revision",
        title="Revision Test",
        tasks=[
            TaskSpec(id="task-A", title="A", assigned_agent="codex", dependencies=[]),
            TaskSpec(id="task-B", title="B", assigned_agent="claude_code", dependencies=["task-A"])
        ]
    )
    test_engine.create_job(req)
    test_engine.complete_task("task-A", summary="Initial version", agent="codex")

    # Task B is now ready
    assert test_engine.get_task("task-B").status == TaskStatus.READY
    test_engine.claim_task("task-B", agent="claude_code")

    # Claude Code spots bug in Task A and requests revision
    rev = test_engine.request_revision(
        target_task_id="task-A",
        feedback="Bug found in auth header parsing",
        from_agent="claude_code"
    )
    assert rev.status == "open"
    assert rev.target_agent == "codex"

    # Task A must now be REVISION_REQUESTED
    task_a = test_engine.get_task("task-A")
    assert task_a.status == TaskStatus.REVISION_REQUESTED
    assert len(task_a.revisions) == 1

    # Task B should be set back to PENDING (blocked)
    task_b = test_engine.get_task("task-B")
    assert task_b.status == TaskStatus.PENDING

    # Codex fixes task A and completes it again
    test_engine.complete_task(
        task_id="task-A",
        summary="Fixed header parsing bug",
        agent="codex"
    )

    task_a_fixed = test_engine.get_task("task-A")
    assert task_a_fixed.status == TaskStatus.COMPLETED
    assert task_a_fixed.revisions[0].status == "resolved"
    assert task_a_fixed.revisions[0].resolution_summary == "Fixed header parsing bug"

    # Task B should now be unblocked again
    task_b_unblocked = test_engine.get_task("task-B")
    assert task_b_unblocked.status == TaskStatus.READY
