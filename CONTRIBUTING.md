# Contributing to AgnView

Thank you for your interest in contributing to AgnView!

The easiest and most valuable contribution is a new **agent adapter**. It takes
five lines of YAML and no Python. Start there.

---

## 1. Adding an agent adapter (five lines, no Python needed)

AgnView can drive any command-line tool. Adapters are plain YAML — there is no
plugin API, no class to subclass and no code to compile.

Open `~/.agnview/agents.yaml` (AgnView writes a template there on first start)
and add one entry under `agents:`:

```yaml
agents:
  aider:
    display: Aider
    command: ["aider", "--message", "{prompt}", "--yes"]
    cwd: "{workspace}"
```

Restart `agnview serve` and the agent appears in the console's dispatch target
list and can be named as `assigned_agent` in a pipeline.

### Adapter keys

| Key | Required | Description |
| :--- | :--- | :--- |
| `display` | yes | Human-readable label shown in the UI. |
| `command` | yes | Argument list. Not a shell string — each argument is its own item. |
| `cwd` | no | Working directory. Defaults to `{workspace}`. |
| `icon` | no | Lucide icon name. Defaults to `terminal`. |
| `colour` | no | Hex colour for the agent's console tag. Defaults to `#38BDF8`. |
| `enabled` | no | Set `false` to keep a definition without listing it. Defaults to `true`. |

### Placeholders

These are substituted into `command` and `cwd` at dispatch time:

`{prompt}`, `{workspace}`, `{session_id}`, `{model}`, `{effort}`, `{skill}`.

### Contributing your adapter back

If your adapter is for a tool other people use, send it in as a worked example:

1. Fork the repository and create a branch.
2. Add the YAML block to the adapter examples in this file, under this section.
3. Say in the pull request which version of the tool you tested it against, and
   paste a line or two of real console output showing it streaming.

No Python, no tests and no development environment are required for this.

---

## 2. Development setup (for code contributions)

Only needed if you are changing AgnView itself.

1. Clone the repository:
   ```bash
   git clone https://github.com/tlaskar-git/AgnView.git
   cd AgnView
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .\.venv\Scripts\activate
   ```

3. Install development dependencies:
   ```bash
   pip install -e .
   pip install pytest httpx
   ```

4. Run tests:
   ```bash
   pytest
   ```

5. Run the server against your checkout:
   ```bash
   agnview serve --port 8765
   ```

## Pull Request Guidelines

- Ensure all existing and new unit tests pass before submitting.
- Follow PEP 8 style conventions and UK English spelling in documentation.
- Maintain minimal and focused diffs.
- Avoid introducing unnecessary external dependencies.

## Licence

By contributing to AgnView, you agree that your contributions will be licensed under the project MIT Licence.
