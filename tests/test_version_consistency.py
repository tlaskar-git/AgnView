"""One version everywhere. A stale copy must fail here, before a release."""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent_relay import __version__
from agent_relay.api.app import create_app

ROOT = Path(__file__).resolve().parent.parent
LITERAL = re.compile(r"""["']\d+\.\d+\.\d+["']""")


def _pyproject_version() -> str:
    match = re.search(r'^version = "(.+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert match
    return match.group(1)


def test_pyproject_matches_the_package_version():
    assert _pyproject_version() == __version__


@pytest.mark.parametrize("relative", ["api/app.py", "core/pairing.py", "mcp/server.py"])
def test_no_second_copy_of_the_version_in_code(relative):
    text = (ROOT / "agent_relay" / relative).read_text(encoding="utf-8")
    # Any quoted x.y.z literal must equal the current version. A quoted date
    # such as the Anthropic API version does not match this pattern.
    for literal in LITERAL.findall(text):
        assert literal.strip("\"'") == __version__, f"{relative} carries {literal}, the package is {__version__}"
    assert "__version__" in text, f"{relative} does not use the package version"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(db_path=str(tmp_path / "version.db")))


def test_openapi_reports_the_version(client):
    assert client.get("/openapi.json").json()["info"]["version"] == __version__


def test_dashboard_shows_the_real_version(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    body = response.text
    assert f">v{__version__}</span>" in body
    assert "v2.4" not in body
    assert "{{AGNVIEW_VERSION}}" not in body


def test_dashboard_template_has_only_the_token():
    template = (ROOT / "agent_relay" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "v{{AGNVIEW_VERSION}}" in template
    assert "v2.4" not in template


def test_dashboard_version_is_escaped_and_only_the_exact_token_is_replaced(tmp_path, monkeypatch):
    from agent_relay.api import app as app_module

    page = tmp_path / "index.html"
    page.write_text("<b>v{{AGNVIEW_VERSION}}</b><i>{{ AGNVIEW_VERSION }}</i><script>var t='{{AGNVIEW_VERSION}}';</script>", encoding="utf-8")
    monkeypatch.setattr(app_module, "__version__", '1.0"><script>')
    out = app_module._render_index(page)
    assert "v1.0&quot;&gt;&lt;script&gt;</b>" in out
    assert "{{ AGNVIEW_VERSION }}" in out
    assert "var t='1.0&quot;&gt;&lt;script&gt;'" in out


def test_pairing_payload_carries_the_version(tmp_path, monkeypatch):
    from agent_relay.core import pairing

    monkeypatch.setattr(pairing, "TOKEN_DIR", tmp_path)
    monkeypatch.setattr(pairing, "TOKEN_FILE", tmp_path / "pairing_token")
    monkeypatch.setattr(pairing, "PAIR_ID_FILE", tmp_path / "pair_id")
    name = next(n for n in dir(pairing) if n.startswith("build_pairing") and "qr" not in n and "uri" not in n)
    payload = getattr(pairing, name)(primary_url="http://192.0.2.1:8765", endpoints={}, token="t", port=8765)
    assert payload["version"] == __version__


def test_mcp_server_reports_the_version():
    text = (ROOT / "agent_relay" / "mcp" / "server.py").read_text(encoding="utf-8")
    assert '"version": __version__' in text


def test_cli_version_flag(monkeypatch, capsys):
    from agent_relay.cli import main as cli

    monkeypatch.setattr(sys, "argv", ["agnview", "--version"])
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == f"agnview {__version__}"


def test_windows_version_file_is_generated_from_the_package_version(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("make_version_file", ROOT / "tools" / "make-version-file.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.read_version() == __version__
    out = tmp_path / "version.txt"
    monkeypatch.setattr(sys, "argv", ["make-version-file.py", str(out)])
    assert module.main() == 0
    text = out.read_text(encoding="utf-8")
    assert f"StringStruct('FileVersion', '{__version__}')" in text
    assert f"StringStruct('ProductVersion', '{__version__}')" in text
    parts = ", ".join(str(int(p)) for p in (__version__.split(".") + ["0"])[:4])
    assert f"filevers=({parts})" in text
    # PyInstaller evaluates the file as Python, so it must at least parse.
    compile(text, "version.txt", "eval")


def test_macos_build_reads_the_single_source():
    script = (ROOT / "tools" / "build-macos.sh").read_text(encoding="utf-8")
    assert "agent_relay/__init__.py" in script
    assert "CFBundleShortVersionString" in script and "CFBundleVersion" in script
