"""Standard Model Context Protocol (MCP) Server for AgentRelay.

Allows Claude Code and AntiGravity to natively coordinate tasks, wait on dependencies,
report task completions, and request revisions via MCP tool calls.
"""

import sys
import json
from typing import Dict, Any

from ..core.engine import RelayEngine
from ..core.db import Database


class AgentRelayMCPServer:
    def __init__(self):
        self.engine = RelayEngine(Database())
        self.tools = [
            {
                "name": "relay_check_job_status",
                "description": "Check the status of a job and all its subtasks in AgentRelay.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "description": "The unique ID of the job"}
                    },
                    "required": ["job_id"]
                }
            },
            {
                "name": "relay_wait_dependencies",
                "description": "Check if upstream task dependencies are satisfied. Returns status and all upstream output summaries.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "The task ID you want to verify readiness for"}
                    },
                    "required": ["task_id"]
                }
            },
            {
                "name": "relay_claim_task",
                "description": "Claim a task to signify that you (Claude Code, AntiGravity, Codex) are actively working on it.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "The task ID to claim"},
                        "agent": {"type": "string", "description": "Your agent identifier (e.g. claude_code, antigravity, codex)"}
                    },
                    "required": ["task_id", "agent"]
                }
            },
            {
                "name": "relay_complete_task",
                "description": "Mark a task completed with your work summary and created artifacts. Automatically unblocks downstream tasks.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "The task ID you completed"},
                        "summary": {"type": "string", "description": "Detailed explanation of what you built/tested"},
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "File paths or artifacts created"
                        },
                        "agent": {"type": "string", "description": "Your agent name"}
                    },
                    "required": ["task_id", "summary"]
                }
            },
            {
                "name": "relay_request_revision",
                "description": "Request a fix/revision on an upstream task (e.g., if Claude Code finds an issue in Task A done by Codex). Blocks downstream tasks until fixed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "target_task_id": {"type": "string", "description": "The upstream task ID with the issue (e.g., task-A)"},
                        "feedback": {"type": "string", "description": "Detailed explanation of what is wrong and how to fix it"},
                        "from_agent": {"type": "string", "description": "Your agent identifier (e.g., claude_code)"}
                    },
                    "required": ["target_task_id", "feedback", "from_agent"]
                }
            }
        ]

    def handle_tool_call(self, name: str, args: Dict[str, Any]) -> str:
        if name == "relay_check_job_status":
            job = self.engine.get_job(args["job_id"])
            return json.dumps(job.model_dump(), indent=2)

        elif name == "relay_wait_dependencies":
            wait_res = self.engine.get_task_wait_status(args["task_id"])
            return json.dumps(wait_res.model_dump(), indent=2)

        elif name == "relay_claim_task":
            task = self.engine.claim_task(args["task_id"], args["agent"])
            return json.dumps({
                "status": "claimed",
                "task_id": task.id,
                "assigned_agent": task.assigned_agent,
                "current_status": task.status.value
            }, indent=2)

        elif name == "relay_complete_task":
            task = self.engine.complete_task(
                task_id=args["task_id"],
                summary=args["summary"],
                artifacts=args.get("artifacts", []),
                agent=args.get("agent")
            )
            return json.dumps({
                "status": "completed",
                "task_id": task.id,
                "summary": task.output_summary,
                "artifacts": task.artifacts
            }, indent=2)

        elif name == "relay_request_revision":
            rev = self.engine.request_revision(
                target_task_id=args["target_task_id"],
                feedback=args["feedback"],
                from_agent=args["from_agent"]
            )
            return json.dumps({
                "status": "revision_requested",
                "revision_id": rev.id,
                "target_task_id": rev.task_id,
                "target_agent": rev.target_agent,
                "feedback": rev.feedback
            }, indent=2)

        else:
            raise ValueError(f"Unknown tool: {name}")

    def run_stdio(self):
        """Standard JSON-RPC 2.0 loop over stdin / stdout for MCP."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                req_id = msg.get("id")
                method = msg.get("method")

                if method == "initialize":
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {
                                "tools": {}
                            },
                            "serverInfo": {
                                "name": "AgentRelay MCP Server",
                                "version": "0.1.11"
                            }
                        }
                    }
                elif method == "notifications/initialized":
                    continue
                elif method == "tools/list":
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "tools": self.tools
                        }
                    }
                elif method == "tools/call":
                    params = msg.get("params", {})
                    tool_name = params.get("name")
                    tool_args = params.get("arguments", {})
                    try:
                        result_text = self.handle_tool_call(tool_name, tool_args)
                        response = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": [
                                    {"type": "text", "text": result_text}
                                ]
                            }
                        }
                    except Exception as err:
                        response = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {
                                "code": -32000,
                                "message": str(err)
                            }
                        }
                else:
                    response = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": f"Method '{method}' not found"
                        }
                    }

                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()

            except Exception as e:
                err_resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": f"Parse error: {str(e)}"
                    }
                }
                sys.stdout.write(json.dumps(err_resp) + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    server = AgentRelayMCPServer()
    server.run_stdio()
