"""Database layer for AgentRelay using SQLite."""

import json
import sqlite3
import os
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.environ.get(
    "AGENT_RELAY_DB_PATH",
    str(Path.home() / ".agent_relay" / "relay.db")
)


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DEFAULT_DB_PATH
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for high concurrency between CLI, server, and workers
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    assigned_agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    from_agent TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    instance_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    hostname TEXT,
                    account TEXT,
                    status TEXT NOT NULL,
                    last_heartbeat TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS usage_accounts (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    name TEXT NOT NULL,
                    auth_type TEXT NOT NULL,
                    credential TEXT NOT NULL,
                    org_id TEXT,
                    plan_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_checked TEXT NOT NULL,
                    data_json TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS console_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent TEXT NOT NULL,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    session_id TEXT,
                    metadata_json TEXT
                );
            """)
            # Remembers the CLI's OWN session id for each (agent, working directory)
            # pair so a follow-up dispatch resumes the same conversation instead of
            # starting a stateless one-shot. This is the real resume token, unlike
            # console_logs.session_id which is only a cosmetic UI grouping label.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    agent TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    cli_session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (agent, cwd)
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS saved_prompts (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    category TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)
            conn.commit()

    # Jobs
    def save_job(self, job_dict: Dict[str, Any]):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO jobs (id, title, description, status, created_at, updated_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    data_json=excluded.data_json
            """, (
                job_dict["id"],
                job_dict["title"],
                job_dict.get("description", ""),
                job_dict["status"],
                job_dict["created_at"],
                job_dict["updated_at"],
                json.dumps(job_dict)
            ))
            conn.commit()

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT data_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row:
                return json.loads(row["data_json"])
            return None

    def list_jobs(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT data_json FROM jobs ORDER BY updated_at DESC").fetchall()
            return [json.loads(row["data_json"]) for row in rows]

    def delete_job(self, job_id: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            conn.commit()
            return cur.rowcount > 0

    # Tasks
    def save_task(self, task_dict: Dict[str, Any]):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO tasks (id, job_id, assigned_agent, status, created_at, updated_at, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    job_id=excluded.job_id,
                    assigned_agent=excluded.assigned_agent,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    data_json=excluded.data_json
            """, (
                task_dict["id"],
                task_dict["job_id"],
                task_dict["assigned_agent"],
                task_dict["status"],
                task_dict["created_at"],
                task_dict["updated_at"],
                json.dumps(task_dict)
            ))
            conn.commit()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT data_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row:
                return json.loads(row["data_json"])
            return None

    def list_tasks_by_job(self, job_id: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT data_json FROM tasks WHERE job_id = ? ORDER BY id ASC", (job_id,)).fetchall()
            return [json.loads(row["data_json"]) for row in rows]

    def list_tasks_by_agent(self, agent: str) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT data_json FROM tasks WHERE assigned_agent = ? ORDER BY updated_at DESC", (agent,)).fetchall()
            return [json.loads(row["data_json"]) for row in rows]

    # Events
    def log_event(self, job_id: str, event_type: str, payload: Dict[str, Any]):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO events (job_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
            """, (job_id, event_type, json.dumps(payload), datetime.now(timezone.utc).isoformat()))
            conn.commit()

    def get_recent_events(self, limit: int = 50, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if job_id:
                rows = conn.execute(
                    "SELECT id, job_id, event_type, payload_json, created_at FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                    (job_id, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, job_id, event_type, payload_json, created_at FROM events ORDER BY id DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return [
                {
                    "id": row["id"],
                    "job_id": row["job_id"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"]
                }
                for row in rows
            ]

    # Agents / Nodes
    def save_agent(self, agent_dict: Dict[str, Any]):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO agents (instance_id, role, hostname, account, status, last_heartbeat, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET
                    role=excluded.role,
                    hostname=excluded.hostname,
                    account=excluded.account,
                    status=excluded.status,
                    last_heartbeat=excluded.last_heartbeat,
                    data_json=excluded.data_json
            """, (
                agent_dict["instance_id"],
                agent_dict["role"],
                agent_dict.get("hostname", ""),
                agent_dict.get("account", ""),
                agent_dict.get("status", "online"),
                agent_dict["last_heartbeat"],
                json.dumps(agent_dict)
            ))
            conn.commit()

    def get_agent(self, instance_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT data_json FROM agents WHERE instance_id = ?", (instance_id,)).fetchone()
            if row:
                return json.loads(row["data_json"])
            return None

    def list_agents(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT data_json FROM agents ORDER BY last_heartbeat DESC").fetchall()
            return [json.loads(row["data_json"]) for row in rows]

    def delete_agent(self, instance_id: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM agents WHERE instance_id = ?", (instance_id,))
            conn.commit()
            return cur.rowcount > 0

    # Subscription Usage Accounts
    def save_usage_account(self, account_dict: Dict[str, Any]):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO usage_accounts (id, provider, name, auth_type, credential, org_id, plan_name, status, last_checked, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    provider=excluded.provider,
                    name=excluded.name,
                    auth_type=excluded.auth_type,
                    credential=excluded.credential,
                    org_id=excluded.org_id,
                    plan_name=excluded.plan_name,
                    status=excluded.status,
                    last_checked=excluded.last_checked,
                    data_json=excluded.data_json
            """, (
                account_dict["id"],
                account_dict["provider"],
                account_dict["name"],
                account_dict.get("auth_type", "api_key"),
                account_dict.get("credential", ""),
                account_dict.get("org_id"),
                account_dict.get("plan_name", "Pro"),
                account_dict.get("status", "active"),
                account_dict["last_checked"],
                json.dumps(account_dict)
            ))
            conn.commit()

    def get_usage_account(self, account_id: str) -> Optional[Dict[str, Any]]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT data_json FROM usage_accounts WHERE id = ?", (account_id,)).fetchone()
            if row:
                return json.loads(row["data_json"])
            return None

    def list_usage_accounts(self, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if provider:
                rows = conn.execute(
                    "SELECT data_json FROM usage_accounts WHERE provider = ? ORDER BY provider ASC, name ASC",
                    (provider,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data_json FROM usage_accounts ORDER BY provider ASC, name ASC"
                ).fetchall()
            return [json.loads(row["data_json"]) for row in rows]

    def delete_usage_account(self, account_id: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM usage_accounts WHERE id = ?", (account_id,))
            conn.commit()
            return cur.rowcount > 0

    # Console Logs
    def add_console_log(self, agent: str, source: str, content: str, session_id: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO console_logs (agent, source, content, timestamp, session_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                agent,
                source,
                content,
                now,
                session_id,
                json.dumps(metadata or {})
            ))
            conn.commit()
            return cur.lastrowid

    def update_console_log(self, log_id: int, content: str) -> bool:
        """Replace the text of one console row.

        A streaming turn writes its row once and then rewrites it as more text
        arrives, so the console keeps one growing bubble per reply and the
        stored row ends up holding the complete final text.
        """
        with self._get_connection() as conn:
            cur = conn.execute(
                "UPDATE console_logs SET content = ? WHERE id = ?",
                (content, log_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_console_logs(
        self,
        agent: Optional[str] = None,
        limit: int = 250,
        session_id: Optional[str] = None,
        after_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Return console log rows.

        ``after_id`` makes hydration incremental: a client that already holds
        the buffer up to a known row id asks only for what came after it,
        instead of pulling the whole buffer back every time.
        """
        with self._get_connection() as conn:
            query = "SELECT * FROM console_logs WHERE 1=1"
            params = []
            if agent and agent != "all":
                query += " AND agent = ?"
                params.append(agent)
            if session_id:
                query += " AND session_id = ?"
                params.append(session_id)
            if after_id is not None:
                query += " AND id > ?"
                params.append(after_id)

            # When a limit trims the result, keep the NEWEST rows. Taking the
            # oldest ones left a client hydrating a long session holding the
            # start of the buffer while the stream delivered the end of it, so
            # its view and the stored buffer could never agree.
            if limit:
                query += " ORDER BY id DESC LIMIT ?"
                params.append(limit)
            else:
                query += " ORDER BY id ASC"

            rows = conn.execute(query, params).fetchall()
            if limit:
                rows = list(reversed(rows))
            logs = []
            for r in rows:
                logs.append({
                    "id": r["id"],
                    "agent": r["agent"],
                    "source": r["source"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                    "session_id": r["session_id"],
                    "metadata": json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                })
            return logs

    def clear_console_logs(self, agent: Optional[str] = None):
        with self._get_connection() as conn:
            if agent and agent != "all":
                conn.execute("DELETE FROM console_logs WHERE agent = ?", (agent,))
            else:
                conn.execute("DELETE FROM console_logs")
            conn.commit()

    # Agent sessions (real CLI resume tokens, keyed on agent + working directory)
    @staticmethod
    def _normalise_session_cwd(cwd: Optional[str]) -> str:
        """Normalise a working directory so lookups are stable.

        Windows paths vary in case and separator, so the same directory must not
        produce two different rows.
        """
        if not cwd:
            return ""
        try:
            return os.path.normcase(os.path.abspath(cwd))
        except Exception:
            return cwd

    def get_agent_session(self, agent: str, cwd: Optional[str]) -> Optional[str]:
        """Return the CLI's own session id last recorded for this agent and directory."""
        key_cwd = self._normalise_session_cwd(cwd)
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT cli_session_id FROM agent_sessions WHERE agent = ? AND cwd = ?",
                (agent, key_cwd)
            ).fetchone()
            return row["cli_session_id"] if row else None

    def set_agent_session(self, agent: str, cwd: Optional[str], cli_session_id: str) -> None:
        """Record the CLI's own session id, replacing any previous one for this pair."""
        if not cli_session_id:
            return
        key_cwd = self._normalise_session_cwd(cwd)
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO agent_sessions (agent, cwd, cli_session_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(agent, cwd) DO UPDATE SET
                    cli_session_id=excluded.cli_session_id,
                    updated_at=excluded.updated_at
            """, (agent, key_cwd, cli_session_id, datetime.now(timezone.utc).isoformat()))
            conn.commit()

    def clear_agent_session(self, agent: str, cwd: Optional[str] = None) -> int:
        """Forget the stored session so the next dispatch starts a fresh conversation."""
        with self._get_connection() as conn:
            if cwd is None:
                cur = conn.execute("DELETE FROM agent_sessions WHERE agent = ?", (agent,))
            else:
                cur = conn.execute(
                    "DELETE FROM agent_sessions WHERE agent = ? AND cwd = ?",
                    (agent, self._normalise_session_cwd(cwd))
                )
            conn.commit()
            return cur.rowcount

    # Saved Prompts
    def save_saved_prompt(self, id: str, title: str, prompt: str, category: str = "custom", created_at: Optional[str] = None) -> Dict[str, Any]:
        with self._get_connection() as conn:
            now_iso = created_at or datetime.now(timezone.utc).isoformat()
            conn.execute("""
                INSERT INTO saved_prompts (id, title, prompt, category, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    prompt=excluded.prompt,
                    category=excluded.category
            """, (id, title, prompt, category, now_iso))
            conn.commit()
            return {"id": id, "title": title, "prompt": prompt, "category": category, "created_at": now_iso}

    def list_saved_prompts(self) -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM saved_prompts ORDER BY created_at ASC").fetchall()
            return [dict(r) for r in rows]

    def delete_saved_prompt(self, prompt_id: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("DELETE FROM saved_prompts WHERE id = ?", (prompt_id,))
            conn.commit()
            return cur.rowcount > 0

