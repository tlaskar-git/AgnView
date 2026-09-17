import os
import pytest
from fastapi.testclient import TestClient
from agent_relay.api.app import create_app


@pytest.fixture
def client(tmp_path):
    db_file = str(tmp_path / "test_fixlist.db")
    app = create_app(db_path=db_file)
    return TestClient(app)


def test_system_capabilities_endpoint(client):
    res = client.get("/api/system/capabilities")
    assert res.status_code == 200
    data = res.json()
    assert "installed_agents" in data
    assert "claude_code" in data["installed_agents"]
    assert "skills" in data
    assert any(s["name"] == "/goal" for s in data["skills"])
    assert "models" in data
    assert "current_cwd" in data
    assert "default_cwd_by_provider" in data
    assert "claude_code" in data["default_cwd_by_provider"]
    assert "codex" in data["default_cwd_by_provider"]
    assert "antigravity" in data["default_cwd_by_provider"]
    assert os.path.exists(data["default_cwd_by_provider"]["claude_code"])
    assert os.path.exists(data["default_cwd_by_provider"]["codex"])
    assert os.path.exists(data["default_cwd_by_provider"]["antigravity"])


def test_system_files_endpoint(client):
    res = client.get("/api/system/files")
    assert res.status_code == 200
    data = res.json()
    assert "files" in data


def test_saved_prompts_crud(client):
    # 1. Get initial seeded prompts
    res = client.get("/api/prompts/saved")
    assert res.status_code == 200
    prompts = res.json()
    assert len(prompts) >= 4

    # 2. Add custom prompt
    new_p = {"title": "Deploy staging", "prompt": "Deploy container to staging cluster"}
    res2 = client.post("/api/prompts/saved", json=new_p)
    assert res2.status_code == 200
    created = res2.json()
    assert created["title"] == "Deploy staging"
    p_id = created["id"]

    # 3. Verify in list
    res3 = client.get("/api/prompts/saved")
    assert any(p["id"] == p_id for p in res3.json())

    # 4. Delete
    res4 = client.delete(f"/api/prompts/saved/{p_id}")
    assert res4.status_code == 200


def test_autostart_endpoint(client):
    res = client.get("/api/system/autostart")
    assert res.status_code == 200
    assert "enabled" in res.json()


