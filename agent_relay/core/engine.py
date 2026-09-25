"""Core state machine and orchestration engine for AgentRelay."""

import asyncio
import logging
import threading
import uuid
from typing import Dict, List, Optional, Any, Set
from pathlib import Path
from datetime import datetime, timezone

from .models import (
    Job, Task, TaskStatus, JobStatus,
    RevisionFeedback, CreateJobRequest, TaskWaitResponse,
    AgentInstance, AgentHeartbeatRequest
)
from .db import Database
from .capabilities import validate_task_options
from .runner import AgentRunner
from .adapters import AdapterManager
from .notifications import NotificationManager

logger = logging.getLogger("agent_relay.engine")

# Broadcast event types that also fire notifications. pipeline_complete and
# pipeline_failed are dispatched explicitly at their state transitions.
NOTIFY_ON_BROADCAST = {
    "task_completed",
    "task_failed",
    "agent_finished",
    "revision_requested",
    "job_completed",
}


def _get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DependencyCycleError(Exception):
    pass


class InvalidStateError(Exception):
    pass


class NotFoundError(Exception):
    pass


class DuplicateIdError(ValueError):
    """A job or task id that already exists, refused when replacing is not allowed."""


class RelayEngine:
    def __init__(self, db: Optional[Database] = None, port: int = 8765, agents_file: Optional[Path] = None):
        self.db = db or Database()
        self.port = port
        self.adapter_manager = AdapterManager(config_path=agents_file)
        self._event_listeners: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.runner = AgentRunner(self.db, broadcast_callback=self.broadcast_event, adapter_manager=self.adapter_manager)
        self.notification_manager = NotificationManager()
        # Makes "is this id taken" and "save it" one step, so two creates that
        # race cannot both pass the check.
        self._create_lock = threading.Lock()

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the serving event loop.

        Most job and task routes are declared with plain ``def``, so FastAPI runs
        them in a worker thread where ``asyncio.get_running_loop()`` raises.
        Without a remembered loop every broadcast from those routes was dropped
        and SSE clients never saw job or task changes.
        """
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    # Event Pub/Sub for SSE and live listeners
    def subscribe_events(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._event_listeners.add(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue):
        self._event_listeners.discard(q)

    async def broadcast_event(self, job_id: str, event_type: str, payload: Dict[str, Any]):
        self.db.log_event(job_id, event_type, payload)
        event_data = {
            "job_id": job_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": _get_utc_now_iso()
        }
        self._fanout(event_data)


        # Broadcast events that are also notification triggers.
        if event_type in NOTIFY_ON_BROADCAST:
            ctx = dict(payload)
            ctx.setdefault("pipeline_id", job_id)
            await self.dispatch_notification(event_type, ctx, job_id=job_id)

    def _fanout(self, event_data: Dict[str, Any]) -> None:
        for q in list(self._event_listeners):
            try:
                q.put_nowait(event_data)
            except Exception:
                self._event_listeners.discard(q)

    async def _emit_system_notice(self, job_id: str, content: str):
        """Surface an operational message in the console stream as a system_notice."""
        try:
            log_id = self.db.add_console_log(
                agent="agnview",
                source="system_notice",
                content=content,
                session_id=None,
            )
        except Exception:
            logger.warning(f"Could not persist system_notice: {content}")
            log_id = None

        self._fanout({
            "job_id": job_id,
            "event_type": "agent_output_chunk",
            "payload": {
                "id": log_id,
                "agent": "agnview",
                "source": "system_notice",
                "content": content,
                "timestamp": _get_utc_now_iso(),
                "session_id": None,
            },
            "created_at": _get_utc_now_iso(),
        })

    async def dispatch_notification(self, event_name: str, context: Dict[str, Any], job_id: str = "global"):
        """Send a notification for event_name and surface any final delivery failure."""
        if not getattr(self, "notification_manager", None):
            return []
        try:
            results = await self.notification_manager.notify_event(event_name, context)
        except Exception as e:
            logger.exception(f"Notification dispatch for '{event_name}' raised")
            await self._emit_system_notice(
                job_id, f"Notification dispatch for '{event_name}' failed: {e}"
            )
            return []

        for res in results:
            if not res.success:
                await self._emit_system_notice(
                    job_id,
                    f"Notification delivery to '{res.channel}' failed after {res.attempts} attempt(s) "
                    f"for event '{event_name}': {res.error or 'unknown error'}",
                )
        return results

    def _notify(self, event_name: str, context: Dict[str, Any], job_id: str = "global"):
        """Schedule a notification from a sync context."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(f"No running loop; skipping notification for '{event_name}'")
            return
        loop.create_task(self.dispatch_notification(event_name, context, job_id=job_id))

    def _sync_broadcast(self, job_id: str, event_type: str, payload: Dict[str, Any]):
        """Safe broadcast from sync contexts, including FastAPI worker threads."""
        self.db.log_event(job_id, event_type, payload)
        event_data = {
            "job_id": job_id,
            "event_type": event_type,
            "payload": payload,
            "created_at": _get_utc_now_iso()
        }

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # Already on the loop thread, deliver straight away.
            self._fanout(event_data)
            return

        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._fanout, event_data)
                return
            except RuntimeError:
                pass

        # No loop is serving; the event is still durably recorded above.
        self._fanout(event_data)

    # Job Management
    def create_job(self, req: CreateJobRequest, allow_replace: bool = True) -> Job:
        """Create a job. The database saves by id, so a job or task id that
        already exists is replaced, which the dashboard relies on. With
        allow_replace False such an id raises DuplicateIdError and nothing is
        saved, so a caller cannot overwrite or take over an existing job or
        task."""
        with self._create_lock:
            return self._create_job(req, allow_replace)

    def _create_job(self, req: CreateJobRequest, allow_replace: bool) -> Job:
        job_id = req.id or f"job-{uuid.uuid4().hex[:8]}"
        now = _get_utc_now_iso()

        if not (req.title or "").strip():
            raise ValueError("Job title cannot be empty.")

        if not req.tasks:
            raise ValueError("A job must contain at least one task.")

        for t in req.tasks:
            if not (t.title or "").strip():
                raise ValueError(f"Task '{t.id}' must have a title.")

        # Check DAG cycle & dependency validity
        task_ids = {t.id for t in req.tasks}
        if len(task_ids) != len(req.tasks):
            raise ValueError("Duplicate task IDs detected in job specification.")

        adj: Dict[str, List[str]] = {t.id: [] for t in req.tasks}
        for t in req.tasks:
            for dep in t.dependencies:
                if dep not in task_ids:
                    raise ValueError(f"Task '{t.id}' references non-existent dependency '{dep}'.")
                adj[dep].append(t.id)

        # Cycle detection using topological sort / DFS
        visited: Dict[str, int] = {tid: 0 for tid in task_ids} # 0=unvisited, 1=visiting, 2=visited
        def dfs(node: str):
            visited[node] = 1
            for neighbor in adj[node]:
                if visited[neighbor] == 1:
                    raise DependencyCycleError(f"Dependency cycle detected involving '{node}' and '{neighbor}'.")
                if visited[neighbor] == 0:
                    dfs(neighbor)
            visited[node] = 2

        for tid in task_ids:
            if visited[tid] == 0:
                dfs(tid)

        # Validate assigned agents against the loaded adapters. This replaces the
        # old static role list: the adapters in agents.yaml are now the
        # only authority on which agents this build coordinates.
        for t in req.tasks:
            if not self.adapter_manager.is_valid_role(t.assigned_agent):
                known = ', '.join(sorted(self.adapter_manager.adapters))
                raise ValueError(
                    f"Task '{t.id}' is assigned to unknown agent "
                    f"'{t.assigned_agent}'. Known agents: {known}."
                )

        for t in req.tasks:
            validate_task_options(t.id, t.assigned_agent, t.model, t.effort)

        if not allow_replace:
            if self.db.get_job(job_id) is not None:
                raise DuplicateIdError(f"Job '{job_id}' already exists.")
            for t in req.tasks:
                if self.db.get_task(t.id) is not None:
                    raise DuplicateIdError(f"Task '{t.id}' already exists.")

        job = Job(
            id=job_id,
            title=req.title,
            description=req.description,
            status=JobStatus.PENDING,
            tasks={},
            created_at=now,
            updated_at=now
        )
        self.db.save_job(job.model_dump())

        # Create tasks
        tasks: Dict[str, Task] = {}
        for t in req.tasks:
            initial_status = TaskStatus.READY if len(t.dependencies) == 0 else TaskStatus.PENDING
            task_obj = Task(
                id=t.id,
                job_id=job_id,
                title=t.title,
                description=t.description,
                assigned_agent=t.assigned_agent,
                dependencies=t.dependencies,
                model=t.model,
                effort=t.effort,
                files=t.files,
                status=initial_status,
                created_at=now,
                updated_at=now
            )
            tasks[t.id] = task_obj
            self.db.save_task(task_obj.model_dump())

        job.tasks = tasks
        self.db.save_job(job.model_dump())
        self._sync_broadcast(job_id, "job_created", {"job_id": job_id, "title": job.title})
        return job

    def get_job(self, job_id: str) -> Job:
        data = self.db.get_job(job_id)
        if not data:
            raise NotFoundError(f"Job '{job_id}' not found.")
        # Reload latest tasks
        task_list = self.db.list_tasks_by_job(job_id)
        tasks_dict = {t["id"]: Task(**t) for t in task_list}
        data["tasks"] = tasks_dict
        return Job(**data)

    def list_jobs(self) -> List[Job]:
        jobs_data = self.db.list_jobs()
        jobs = []
        for j in jobs_data:
            task_list = self.db.list_tasks_by_job(j["id"])
            j["tasks"] = {t["id"]: Task(**t) for t in task_list}
            jobs.append(Job(**j))
        return jobs

    def delete_job(self, job_id: str) -> bool:
        deleted = self.db.delete_job(job_id)
        if deleted:
            self._sync_broadcast(job_id, "job_deleted", {"job_id": job_id})
        return deleted

    # Task Management
    def get_task(self, task_id: str) -> Task:
        data = self.db.get_task(task_id)
        if not data:
            raise NotFoundError(f"Task '{task_id}' not found.")
        return Task(**data)

    def claim_task(self, task_id: str, agent: str, instance_id: Optional[str] = None) -> Task:
        task = self.get_task(task_id)

        if task.status not in (TaskStatus.READY, TaskStatus.REVISION_REQUESTED, TaskStatus.IN_PROGRESS):
            raise InvalidStateError(
                f"Cannot claim task '{task_id}' in state '{task.status.value}'. Dependencies may still be pending."
            )

        # Multi-node / multi-account matching logic:
        # If task.assigned_agent is specific (e.g. 'codex@alice-laptop'), only that instance or matching role/account may claim it.
        effective_instance = instance_id or agent
        assigned = task.assigned_agent

        if "@" in assigned:
            # Explicit assignment to specific machine/account
            if effective_instance != assigned and agent != assigned:
                raise InvalidStateError(
                    f"Task '{task_id}' is explicitly assigned to '{assigned}', but claimed by '{effective_instance}'."
                )

        task.status = TaskStatus.IN_PROGRESS
        task.executed_by = effective_instance
        task.updated_at = _get_utc_now_iso()
        self.db.save_task(task.model_dump())

        # Update job status
        job = self.get_job(task.job_id)
        if job.status == JobStatus.PENDING:
            job.status = JobStatus.IN_PROGRESS
            job.updated_at = _get_utc_now_iso()
            self.db.save_job(job.model_dump())

        self._sync_broadcast(task.job_id, "task_claimed", {
            "task_id": task_id,
            "agent": agent,
            "instance_id": effective_instance,
            "status": task.status.value
        })
        return task

    def complete_task(
        self,
        task_id: str,
        summary: str,
        artifacts: Optional[List[str]] = None,
        agent: Optional[str] = None,
        instance_id: Optional[str] = None
    ) -> Task:
        task = self.get_task(task_id)
        now = _get_utc_now_iso()

        task.status = TaskStatus.COMPLETED
        task.output_summary = summary
        if artifacts:
            task.artifacts = list(set(task.artifacts + artifacts))
        if instance_id:
            task.executed_by = instance_id
        elif agent:
            task.executed_by = task.executed_by or agent
        task.updated_at = now
        task.completed_at = now

        # If any open revisions exist on this task, mark them resolved
        for rev in task.revisions:
            if rev.status == "open":
                rev.status = "resolved"
                rev.resolved_at = now
                rev.resolution_summary = summary

        self.db.save_task(task.model_dump())

        # Re-evaluate all tasks in the job to unlock downstream dependents
        job = self.get_job(task.job_id)
        all_tasks = self.db.list_tasks_by_job(job.id)
        task_map = {t["id"]: Task(**t) for t in all_tasks}
        task_map[task_id] = task

        unlocked_tasks = []
        for other_id, other_task in task_map.items():
            if other_id == task_id:
                continue
            # If other task was pending, check if all its dependencies are now completed
            if other_task.status == TaskStatus.PENDING:
                all_deps_met = True
                for dep_id in other_task.dependencies:
                    dep_task = task_map.get(dep_id)
                    if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                        all_deps_met = False
                        break
                if all_deps_met:
                    other_task.status = TaskStatus.READY
                    other_task.updated_at = now
                    self.db.save_task(other_task.model_dump())
                    unlocked_tasks.append(other_id)

        # Check if entire job is now completed
        all_completed = all(t.status == TaskStatus.COMPLETED for t in task_map.values())
        has_revision = any(t.status == TaskStatus.REVISION_REQUESTED for t in task_map.values())

        if all_completed:
            job.status = JobStatus.COMPLETED
        elif has_revision:
            job.status = JobStatus.REVISION_IN_PROGRESS
        else:
            job.status = JobStatus.IN_PROGRESS

        job.updated_at = now
        self.db.save_job(job.model_dump())

        self._sync_broadcast(job.id, "task_completed", {
            "task_id": task_id,
            "task_title": task.title,
            "pipeline_title": job.title,
            "status": "completed",
            "agent": task.assigned_agent,
            "summary": summary,
            "unlocked_tasks": unlocked_tasks,
            "job_status": job.status.value
        })

        if all_completed:
            self._sync_broadcast(job.id, "job_completed", {
                "pipeline_id": job.id,
                "pipeline_title": job.title,
                "status": "completed",
                "duration": "completed",
            })
            self._notify("pipeline_complete", {
                "pipeline_id": job.id,
                "pipeline_title": job.title,
                "status": "completed",
                "duration": "completed",
            }, job_id=job.id)

        return task

    def _transitive_dependents(self, task_map: Dict[str, Task], root_id: str) -> Set[str]:
        dependents: Set[str] = set()
        queue = [root_id]
        while queue:
            curr = queue.pop(0)
            for tid, t in task_map.items():
                if curr in t.dependencies and tid not in dependents:
                    dependents.add(tid)
                    queue.append(tid)
        return dependents

    def fail_task(
        self,
        task_id: str,
        reason: str,
        agent: Optional[str] = None,
        instance_id: Optional[str] = None
    ) -> Task:
        """Mark a task failed, block everything downstream of it, and fail the job.

        Without this the 'failed' and 'blocked' statuses were documented but
        unreachable: nothing in the API or the dashboard could produce them.
        """
        task = self.get_task(task_id)
        if task.status == TaskStatus.COMPLETED:
            raise InvalidStateError(
                f"Cannot fail task '{task_id}' because it is already completed."
            )

        now = _get_utc_now_iso()
        task.status = TaskStatus.FAILED
        task.output_summary = reason
        if instance_id:
            task.executed_by = instance_id
        elif agent:
            task.executed_by = task.executed_by or agent
        task.updated_at = now
        self.db.save_task(task.model_dump())

        job = self.get_job(task.job_id)
        all_tasks = self.db.list_tasks_by_job(job.id)
        task_map = {t["id"]: Task(**t) for t in all_tasks}
        task_map[task_id] = task

        blocked_tasks = []
        for dep_id in self._transitive_dependents(task_map, task_id):
            dep_task = task_map[dep_id]
            if dep_task.status in (
                TaskStatus.PENDING, TaskStatus.READY,
                TaskStatus.IN_PROGRESS, TaskStatus.REVISION_REQUESTED
            ):
                dep_task.status = TaskStatus.BLOCKED
                dep_task.updated_at = now
                self.db.save_task(dep_task.model_dump())
                blocked_tasks.append(dep_id)

        job.status = JobStatus.FAILED
        job.updated_at = now
        self.db.save_job(job.model_dump())

        self._sync_broadcast(job.id, "task_failed", {
            "task_id": task_id,
            "task_title": task.title,
            "pipeline_title": job.title,
            "status": "failed",
            "agent": task.assigned_agent,
            "reason": reason,
            "blocked_tasks": blocked_tasks,
            "job_status": job.status.value
        })

        self._notify("pipeline_failed", {
            "pipeline_id": job.id,
            "pipeline_title": job.title,
            "status": "failed",
            "failed_task": task.title or task_id,
            "error": reason
        }, job_id=job.id)

        return task

    def request_revision(self, target_task_id: str, feedback: str, from_agent: str) -> RevisionFeedback:
        """
        Claude Code or any agent spots a defect in an upstream task (e.g. Task A Codex or Task B AntiGravity).
        Marks the target task as 'revision_requested', adds feedback, and blocks downstream tasks.
        """
        target_task = self.get_task(target_task_id)
        now = _get_utc_now_iso()

        rev_id = f"rev-{uuid.uuid4().hex[:6]}"
        rev = RevisionFeedback(
            id=rev_id,
            task_id=target_task_id,
            from_agent=from_agent,
            target_agent=target_task.assigned_agent,
            feedback=feedback,
            status="open",
            created_at=now
        )
        target_task.revisions.append(rev)
        target_task.status = TaskStatus.REVISION_REQUESTED
        target_task.updated_at = now
        self.db.save_task(target_task.model_dump())

        # Find all downstream tasks that depend on target_task directly or indirectly
        job = self.get_job(target_task.job_id)
        all_tasks = self.db.list_tasks_by_job(job.id)
        task_map = {t["id"]: Task(**t) for t in all_tasks}
        task_map[target_task_id] = target_task

        dependents = self._transitive_dependents(task_map, target_task_id)

        # Set dependent tasks that were READY back to PENDING since prerequisite needs revision
        blocked_tasks = []
        for dep_id in dependents:
            dep_task = task_map[dep_id]
            if dep_task.status in (TaskStatus.READY, TaskStatus.IN_PROGRESS):
                dep_task.status = TaskStatus.PENDING
                dep_task.updated_at = now
                self.db.save_task(dep_task.model_dump())
                blocked_tasks.append(dep_id)

        job.status = JobStatus.REVISION_IN_PROGRESS
        job.updated_at = now
        self.db.save_job(job.model_dump())

        self._sync_broadcast(job.id, "revision_requested", {
            "target_task_id": target_task_id,
            "task_title": target_task.title,
            "pipeline_title": job.title,
            "status": "revision_requested",
            "target_agent": target_task.assigned_agent,
            "from_agent": from_agent,
            "feedback": feedback,
            "blocked_tasks": blocked_tasks,
            "revision_id": rev_id
        })

        return rev

    def get_task_wait_status(self, task_id: str) -> TaskWaitResponse:
        """
        Inspects if task is ready to start, gathering unmet dependencies and upstream outputs.
        """
        task = self.get_task(task_id)
        job = self.get_job(task.job_id)

        unmet: List[str] = []
        upstream_summaries: Dict[str, Any] = {}

        for dep_id in task.dependencies:
            dep_task = job.tasks.get(dep_id)
            if not dep_task or dep_task.status != TaskStatus.COMPLETED:
                unmet.append(dep_id)
            if dep_task:
                upstream_summaries[dep_id] = {
                    "title": dep_task.title,
                    "agent": dep_task.assigned_agent,
                    "status": dep_task.status.value,
                    "output_summary": dep_task.output_summary,
                    "artifacts": dep_task.artifacts
                }

        open_revisions = [r for r in task.revisions if r.status == "open"]
        is_ready = (len(unmet) == 0) and (task.status in (TaskStatus.READY, TaskStatus.REVISION_REQUESTED, TaskStatus.IN_PROGRESS))

        return TaskWaitResponse(
            task_id=task_id,
            job_id=task.job_id,
            ready=is_ready,
            status=task.status,
            unmet_dependencies=unmet,
            upstream_summaries=upstream_summaries,
            open_revisions=open_revisions,
            task=task
        )

    # ----------------- Agent / Node Registry -----------------

    def record_heartbeat(self, req: AgentHeartbeatRequest) -> AgentInstance:
        now = _get_utc_now_iso()
        instance = AgentInstance(
            instance_id=req.instance_id,
            role=req.role,
            hostname=req.hostname or "",
            account=req.account or "",
            status=req.status or "online",
            current_task_id=req.current_task_id,
            last_heartbeat=now,
            metadata=req.metadata or {}
        )
        self.db.save_agent(instance.model_dump())
        self._sync_broadcast("global", "agent_heartbeat", {
            "instance_id": instance.instance_id,
            "role": instance.role,
            "hostname": instance.hostname,
            "account": instance.account,
            "status": instance.status
        })
        return instance

    def list_agents(self, timeout_seconds: int = 60) -> List[AgentInstance]:
        raw_agents = self.db.list_agents()
        now_dt = datetime.now(timezone.utc)
        result = []

        for raw in raw_agents:
            # Check if last heartbeat is older than timeout
            try:
                hb_dt = datetime.fromisoformat(raw["last_heartbeat"])
                elapsed = (now_dt - hb_dt).total_seconds()
                if elapsed > timeout_seconds and raw.get("status") != "offline":
                    raw["status"] = "offline"
            except Exception:
                pass
            result.append(AgentInstance(**raw))

        return result
