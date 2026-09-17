# Agent Adapters in AgnView

AgnView uses an extensible agent adapter architecture. Any command-line tool, script, or AI assistant can be configured as an agent adapter without modifying AgnView source code.

## Configuration Location

Agent adapters are defined in YAML format at:
`
~/.agnview/agents.yaml
`

If the file does not exist when AgnView starts, a default template is created automatically with built-in adapters (claude_code, codex, ntigravity, gemini).

## Schema

Each entry under the gents root mapping defines one agent:

`yaml
agents:
  <agent_id>:
    display: <Human-readable Name>      # Required. Display label in UI.
    command: [<executable>, <args...>]  # Required. Command argument list.
    cwd: <working_directory>          # Optional. Default: {workspace}.
    icon: <lucide_icon_name>            # Optional. Default: terminal.
    colour: <hex_colour>              # Optional. Default: #38BDF8.
    enabled: true                       # Optional. Default: true.
`

### Available Placeholders

AgnView substitutes runtime parameters into string arguments in command and cwd:

| Placeholder | Description |
| :--- | :--- |
| {prompt} | The prompt text dispatched from the console or task instruction |
| {workspace} | Current working directory or project root |
| {session_id} | Unique session identifier for the execution |
| {model} | Chosen model string (if selected) |
| {effort} | Reasoning effort configuration (if selected) |
| {skill} | Skill or command prefix (if selected) |

## Worked Examples

### 1. Aider

[Aider](https://aider.chat) is a command-line pairing tool:

`yaml
agents:
  aider:
    display: Aider
    command: [aider, --message, {prompt}, --no-auto-commits]
    cwd: {workspace}
    icon: code
    colour: #10B981
    enabled: true
`

### 2. OpenCode

OpenCode or OpenDevin command-line interfaces:

`yaml
agents:
  opencode:
    display: OpenCode
    command: [opencode, run, --task, {prompt}, --dir, {workspace}]
    cwd: {workspace}
    icon: bot
    colour: #6366F1
    enabled: true
`

### 3. Cursor Agent CLI

Cursor CLI or headless editor agent:

`yaml
agents:
  cursor:
    display: Cursor Agent
    command: [cursor, --agent, -p, {prompt}]
    cwd: {workspace}
    icon: compass
    colour: #0EA5E9
    enabled: true
`

### 4. Custom Shell Script

A bespoke bash or shell pipeline:

`yaml
agents:
  code_review_script:
    display: Security Review Script
    command: [bash, -c, python scripts/security_check.py --prompt "{prompt}" --path "{workspace}"]
    cwd: {workspace}
    icon: shield-check
    colour: #F59E0B
    enabled: true
`

## Reloading Adapters

When you edit ~/.agnview/agents.yaml, you can reload your configurations without restarting the AgnView server:
- In the Web Dashboard: click the **Reload Adapters** button in the header toolbar.
- Via REST API: send POST /api/agents/reload.
