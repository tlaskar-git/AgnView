"""Parse guard test: verifies that all HTML template <script> blocks parse cleanly with zero SyntaxErrors."""

import re
import subprocess
from pathlib import Path


def _validate_html_scripts(file_path: Path):
    content = file_path.read_text(encoding="utf-8")
    # Find all inline <script> tags without src=
    scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", content, re.DOTALL | re.IGNORECASE)
    assert len(scripts) > 0, f"Expected inline scripts in {file_path.name}"

    for i, script_code in enumerate(scripts):
        if not script_code.strip():
            continue
        # Use node --check via stdin with utf-8 encoding
        proc = subprocess.run(
            ["node", "--check"],
            input=script_code,
            text=True,
            encoding="utf-8",
            capture_output=True
        )
        assert proc.returncode == 0, f"Script block {i} in {file_path.name} failed syntax check:\n{proc.stderr}"


def test_index_html_scripts_syntax():
    index_path = Path("agent_relay/web/templates/index.html")
    assert index_path.exists()
    _validate_html_scripts(index_path)
