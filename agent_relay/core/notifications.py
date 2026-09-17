"""Notification system for AgnView.

Supports ntfy, Discord, Slack, Telegram, and raw webhooks.
Loads config from ~/.agnview/notifications.yaml with graceful error handling.
"""

import logging
import asyncio
from pathlib import Path
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
import httpx
import yaml

logger = logging.getLogger("agent_relay.notifications")

DEFAULT_CONFIG_PATH = Path.home() / ".agnview" / "notifications.yaml"

# Per-attempt HTTP timeout for webhook delivery (seconds).
DELIVERY_TIMEOUT = 5.0

# Delays before each retry. One initial attempt plus len(RETRY_BACKOFFS) retries.
RETRY_BACKOFFS = (1.0, 2.0, 4.0)

# Events that AgnView dispatches notifications for.
TRIGGER_EVENTS = (
    "task_completed",
    "task_failed",
    "agent_finished",
    "revision_requested",
    "job_completed",
    "pipeline_complete",
    "pipeline_failed",
)

DEFAULT_NOTIFICATIONS_YAML = """# AgnView Notifications Configuration
# Supported channels: ntfy, discord, slack, telegram, raw
# All webhooks support {pipeline_id}, {pipeline_title}, {status}, {duration}, {failed_task}
#
# Available events (or use "all"):
#   task_completed, task_failed, agent_finished, revision_requested,
#   job_completed, pipeline_complete, pipeline_failed
#
# Delivery: 5s timeout per attempt, 3 retries with 1s/2s/4s backoff.
# Exhausted deliveries surface as a system_notice in the console stream.

channels:
  # Example ntfy configuration:
  # ntfy:
  #   topic: agnview-alerts
  #   server: https://ntfy.sh
  #   priority: default
  #   events:
  #     - pipeline_complete
  #     - pipeline_failed

  # Example Discord webhook configuration:
  # discord:
  #   webhook_url: "https://discord.com/api/webhooks/..."
  #   events:
  #     - pipeline_failed

  # Example Slack webhook configuration:
  # slack:
  #   webhook_url: "https://hooks.slack.com/services/..."
  #   events:
  #     - pipeline_failed

  # Example Telegram bot configuration:
  # telegram:
  #   bot_token: "123456:ABC-DEF..."
  #   chat_id: "-1001234567890"
  #   events:
  #     - pipeline_failed

  # Example Raw Webhook:
  # raw:
  #   url: "https://example.com/webhook"
  #   method: POST
  #   headers:
  #     Authorization: "Bearer ..."
  #   events:
  #     - pipeline_complete
  #     - pipeline_failed
"""


class NotificationConfig(BaseModel):
    channels: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class NotificationResult(BaseModel):
    channel: str
    success: bool
    status_code: Optional[int] = None
    error: Optional[str] = None
    attempts: int = 0


class NotificationManager:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.config = NotificationConfig()
        self.load()

    def ensure_default_file(self) -> bool:
        if not self.config_path.exists():
            try:
                self.config_path.parent.mkdir(parents=True, exist_ok=True)
                self.config_path.write_text(DEFAULT_NOTIFICATIONS_YAML, encoding="utf-8")
                return True
            except Exception as e:
                logger.warning(f"Could not create default notifications.yaml: {e}")
        return False

    def load(self) -> NotificationConfig:
        self.ensure_default_file()
        if not self.config_path.exists():
            self.config = NotificationConfig()
            return self.config

        try:
            content = self.config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content) or {}
            if not isinstance(data, dict):
                data = {}
            channels = data.get("channels") or {}
            if not isinstance(channels, dict):
                channels = {}
            self.config = NotificationConfig(channels=channels)
        except Exception as e:
            logger.warning(f"Error loading notifications config from {self.config_path}: {e}")
            self.config = NotificationConfig()

        return self.config

    def get_public_channels(self) -> List[Dict[str, Any]]:
        """Return configured channels with sensitive tokens masked for UI display."""
        results = []
        for name, cfg in self.config.channels.items():
            if not isinstance(cfg, dict):
                continue
            masked = {}
            for k, v in cfg.items():
                val_str = str(v)
                if any(secret_key in k.lower() for secret_key in ("token", "webhook_url", "url", "key", "secret", "password")):
                    if len(val_str) > 12:
                        masked[k] = val_str[:6] + "..." + val_str[-4:]
                    elif val_str:
                        masked[k] = "***"
                    else:
                        masked[k] = ""
                else:
                    masked[k] = v
            results.append({"name": name, "config": masked, "events": cfg.get("events", [])})
        return results

    def _render_placeholders(self, template: str, context: Dict[str, Any]) -> str:
        res = template
        for k, v in context.items():
            res = res.replace(f"{{{k}}}", str(v))
        return res

    def _build_request(self, channel_name: str, channel_cfg: Dict[str, Any], title: str, message: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Build the httpx request for a channel.

        Returns a dict with keys: method, url, headers, and one of content/json.
        Raises ValueError when the channel configuration is incomplete.
        """
        headers = {"User-Agent": "AgnView/2.0"}

        if channel_name == "ntfy" or "topic" in channel_cfg:
            topic = channel_cfg.get("topic", "agnview-alerts")
            server = str(channel_cfg.get("server", "https://ntfy.sh")).rstrip("/")
            priority = channel_cfg.get("priority", "default")
            ntfy_headers = {
                "Title": title,
                "Priority": str(priority),
                "Tags": "warning" if "fail" in str(context.get("status", "")).lower() else "white_check_mark",
            }
            ntfy_headers.update(headers)
            return {
                "method": "POST",
                "url": f"{server}/{topic}",
                "headers": ntfy_headers,
                "content": message.encode("utf-8"),
            }

        if channel_name == "discord" or "discord.com" in str(channel_cfg.get("webhook_url", "")):
            url = channel_cfg.get("webhook_url", "")
            if not url:
                raise ValueError("Missing discord webhook_url")
            payload = {
                "embeds": [{
                    "title": title,
                    "description": message,
                    "color": 0xE02424 if "fail" in str(context.get("status", "")).lower() else 0x10B981,
                    "fields": [
                        {"name": "Pipeline", "value": str(context.get("pipeline_title", context.get("pipeline_id", ""))), "inline": True},
                        {"name": "Status", "value": str(context.get("status", "")).upper(), "inline": True},
                    ],
                }]
            }
            return {"method": "POST", "url": url, "headers": headers, "json": payload}

        if channel_name == "slack" or "hooks.slack.com" in str(channel_cfg.get("webhook_url", "")):
            url = channel_cfg.get("webhook_url", "")
            if not url:
                raise ValueError("Missing slack webhook_url")
            return {"method": "POST", "url": url, "headers": headers, "json": {"text": f"*{title}*\\n{message}"}}

        if channel_name == "telegram" or ("bot_token" in channel_cfg and "chat_id" in channel_cfg):
            bot_token = channel_cfg.get("bot_token", "")
            chat_id = channel_cfg.get("chat_id", "")
            if not bot_token or not chat_id:
                raise ValueError("Missing telegram bot_token or chat_id")
            return {
                "method": "POST",
                "url": f"https://api.telegram.org/bot{bot_token}/sendMessage",
                "headers": headers,
                "json": {"chat_id": chat_id, "text": f"*{title}*\\n{message}", "parse_mode": "Markdown"},
            }

        # Raw generic webhook
        url = channel_cfg.get("url") or channel_cfg.get("webhook_url", "")
        if not url:
            raise ValueError("Missing webhook url")
        all_headers = dict(headers)
        custom_headers = channel_cfg.get("headers", {})
        if isinstance(custom_headers, dict):
            all_headers.update(custom_headers)
        return {
            "method": str(channel_cfg.get("method", "POST")).upper(),
            "url": url,
            "headers": all_headers,
            "json": {
                "event": context.get("event", "alert"),
                "title": title,
                "message": message,
                "context": context,
            },
        }

    async def send_to_channel(self, channel_name: str, channel_cfg: Dict[str, Any], title: str, message: str, context: Dict[str, Any]) -> NotificationResult:
        """Deliver one notification, retrying with RETRY_BACKOFFS between attempts."""
        if not isinstance(channel_cfg, dict):
            return NotificationResult(channel=channel_name, success=False, error="Invalid channel configuration")

        try:
            req = self._build_request(channel_name, channel_cfg, title, message, context)
        except ValueError as e:
            # Configuration errors are not retryable.
            return NotificationResult(channel=channel_name, success=False, error=str(e))

        method = req.pop("method")
        url = req.pop("url")

        last_result = NotificationResult(channel=channel_name, success=False, error="Not attempted")
        total_attempts = len(RETRY_BACKOFFS) + 1

        for attempt in range(1, total_attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT) as client:
                    if method == "POST":
                        resp = await client.post(url, **req)
                    else:
                        resp = await client.request(method, url, **req)
                last_result = NotificationResult(
                    channel=channel_name,
                    success=resp.is_success,
                    status_code=resp.status_code,
                    error=None if resp.is_success else f"HTTP {resp.status_code}",
                    attempts=attempt,
                )
            except Exception as e:
                last_result = NotificationResult(channel=channel_name, success=False, error=str(e), attempts=attempt)

            if last_result.success:
                return last_result

            if attempt <= len(RETRY_BACKOFFS):
                delay = RETRY_BACKOFFS[attempt - 1]
                logger.warning(
                    f"Notification to '{channel_name}' failed (attempt {attempt}/{total_attempts}): "
                    f"{last_result.error}. Retrying in {delay}s."
                )
                await asyncio.sleep(delay)

        logger.error(f"Notification to '{channel_name}' failed after {last_result.attempts} attempts: {last_result.error}")
        return last_result


    async def notify_event(self, event_name: str, context: Dict[str, Any]) -> List[NotificationResult]:
        """Send notification to all channels subscribed to event_name."""
        results = []
        pipeline_title = context.get("pipeline_title") or context.get("pipeline_id", "Pipeline")

        if event_name == "pipeline_failed":
            title = f"Pipeline Failed: {pipeline_title}"
            failed_task = context.get("failed_task", "unknown task")
            message = f"Pipeline '{pipeline_title}' failed on task: {failed_task}. Duration: {context.get('duration', 'N/A')}"
        elif event_name == "pipeline_complete":
            title = f"Pipeline Completed: {pipeline_title}"
            message = f"Pipeline '{pipeline_title}' completed successfully. Duration: {context.get('duration', 'N/A')}"
        elif event_name == "job_completed":
            title = f"Job Completed: {pipeline_title}"
            message = f"All tasks in job '{pipeline_title}' completed. Duration: {context.get('duration', 'N/A')}"
        elif event_name == "task_completed":
            title = f"Task Completed: {context.get('task_title', context.get('task_id', 'task'))}"
            message = (
                f"Task '{context.get('task_title', context.get('task_id', 'task'))}' completed "
                f"in job '{pipeline_title}' (agent: {context.get('agent', 'unknown')})."
            )
        elif event_name == "task_failed":
            title = f"Task Failed: {context.get('task_title', context.get('task_id', 'task'))}"
            message = (
                f"Task '{context.get('task_title', context.get('task_id', 'task'))}' failed "
                f"in job '{pipeline_title}': {context.get('error', 'no error detail')}"
            )
        elif event_name == "agent_finished":
            title = f"Agent Finished: {context.get('agent', 'agent')}"
            message = (
                f"Agent '{context.get('agent', 'agent')}' finished with exit code "
                f"{context.get('exit_code', 'unknown')}. {context.get('summary', '')}".strip()
            )
        elif event_name == "revision_requested":
            title = f"Revision Requested: {context.get('task_title', context.get('target_task_id', 'task'))}"
            message = (
                f"{context.get('from_agent', 'An agent')} requested a revision on task "
                f"'{context.get('target_task_id', 'unknown')}' in job '{pipeline_title}': "
                f"{context.get('feedback', '')}".strip()
            )
        else:
            title = f"AgnView Alert: {event_name}"
            message = f"Event '{event_name}' occurred in AgnView."

        for channel_name, cfg in self.config.channels.items():
            if not isinstance(cfg, dict):
                continue
            events = cfg.get("events", ["pipeline_failed", "pipeline_complete"])
            if event_name in events or "all" in events:
                ctx = dict(context)
                ctx["event"] = event_name
                res = await self.send_to_channel(channel_name, cfg, title, message, ctx)
                results.append(res)

        return results

    async def test_channel(self, channel_name: str, config_override: Optional[Dict[str, Any]] = None) -> NotificationResult:
        """Send a test message to a specific channel."""
        cfg = config_override or self.config.channels.get(channel_name)
        if not cfg:
            return NotificationResult(channel=channel_name, success=False, error=f"Channel '{channel_name}' not configured")

        test_context = {
            "pipeline_id": "job-test",
            "pipeline_title": "AgnView Test Pipeline",
            "status": "completed",
            "duration": "12s",
            "failed_task": "none",
            "event": "test_alert"
        }
        title = "AgnView Test Notification"
        message = "This is a test notification from your AgnView instance."
        return await self.send_to_channel(channel_name, cfg, title, message, test_context)
