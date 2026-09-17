"""The published mobile spec must describe routes the server actually serves.

The spec used to document POST /api/jobs/{job_id}/revisions, which has never
existed. The working path is POST /api/tasks/{task_id}/request-revision.
"""

import json
import os
import tempfile
from pathlib import Path

from agent_relay.api.app import create_app

SPEC = Path(__file__).resolve().parent.parent / "docs" / "mobile-api-spec.json"


def _served_paths():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_app(db_path=path)
    # Read the generated schema rather than app.routes: included routers are
    # not flattened into app.routes in current FastAPI versions.
    return set(app.openapi().get("paths", {}))


def test_every_api_path_in_the_spec_is_served():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    served = _served_paths()

    missing = [
        p for p in spec.get("paths", {})
        if p.startswith("/api/") and p not in served
    ]
    assert not missing, f"spec documents routes the server does not serve: {missing}"


def test_the_revision_path_is_the_task_scoped_one():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    paths = spec.get("paths", {})
    assert "/api/tasks/{task_id}/request-revision" in paths
    assert "/api/jobs/{job_id}/revisions" not in paths


def test_status_enums_match_the_models():
    from agent_relay.core.models import JobStatus, TaskStatus

    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    schemas = spec["components"]["schemas"]

    assert set(schemas["Job"]["properties"]["status"]["enum"]) == {s.value for s in JobStatus}
    assert set(schemas["Task"]["properties"]["status"]["enum"]) == {s.value for s in TaskStatus}
    # Agent roles are dynamic: they come from agents.yaml via the adapter
    # manager, so the spec must not pin them to a fixed enum.
    assigned_agent = schemas["Task"]["properties"]["assigned_agent"]
    assert assigned_agent["type"] == "string"
    assert "enum" not in assigned_agent


def test_revision_in_progress_is_documented():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    job_statuses = spec["components"]["schemas"]["Job"]["properties"]["status"]["enum"]
    assert "revision_in_progress" in job_statuses
