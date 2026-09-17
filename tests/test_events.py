"""Regression tests for SSE fan-out of job and task changes.

Job and task routes are declared with plain ``def``, so FastAPI runs them on a
worker thread. Before the loop binding was added, every broadcast raised inside
``asyncio.get_running_loop()`` and was silently dropped, so connected SSE
clients received nothing.
"""

import asyncio
import tempfile
import os

from agent_relay.core.db import Database
from agent_relay.core.engine import RelayEngine
from agent_relay.core.models import CreateJobRequest, TaskSpec


def _engine():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return RelayEngine(Database(path), port=8799)


async def _broadcast_from_worker_thread():
    engine = _engine()
    engine.bind_loop(asyncio.get_running_loop())
    q = engine.subscribe_events()

    job_req = CreateJobRequest(
        id="evt-job",
        title="Event job",
        tasks=[
            TaskSpec(id="t1", title="One", assigned_agent="codex"),
            TaskSpec(id="t2", title="Two", assigned_agent="claude_code", dependencies=["t1"]),
        ],
    )

    # Drive the engine the way FastAPI does: from a worker thread.
    await asyncio.to_thread(engine.create_job, job_req)
    await asyncio.to_thread(engine.claim_task, "t1", "codex", None)
    await asyncio.to_thread(engine.complete_task, "t1", "done", None, "codex", None)

    seen = []
    for _ in range(3):
        event = await asyncio.wait_for(q.get(), timeout=5.0)
        seen.append(event["event_type"])

    assert seen == ["job_created", "task_claimed", "task_completed"]


async def _every_subscriber_receives_same_event():
    engine = _engine()
    engine.bind_loop(asyncio.get_running_loop())
    first = engine.subscribe_events()
    second = engine.subscribe_events()

    await asyncio.to_thread(
        engine.create_job,
        CreateJobRequest(
            id="evt-job-2",
            title="Second job",
            tasks=[TaskSpec(id="s1", title="Only", assigned_agent="codex")],
        ),
    )

    a = await asyncio.wait_for(first.get(), timeout=5.0)
    b = await asyncio.wait_for(second.get(), timeout=5.0)
    assert a["event_type"] == b["event_type"] == "job_created"


def test_broadcast_from_worker_thread_reaches_subscriber():
    asyncio.run(_broadcast_from_worker_thread())


def test_every_subscriber_receives_the_same_event():
    asyncio.run(_every_subscriber_receives_same_event())
