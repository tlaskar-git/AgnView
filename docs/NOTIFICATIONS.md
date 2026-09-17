# Notifications in AgnView

AgnView can dispatch instant webhook notifications when multi-agent pipelines complete or when any task encounters a failure.

## Configuration Location

Notification channels are configured in YAML format at:
`
~/.agnview/notifications.yaml
`

If the file does not exist when AgnView starts, a default template is automatically created.

## Supported Notification Channels

### 1. ntfy (Recommended)

[ntfy.sh](https://ntfy.sh) is a free, open-source HTTP-based pub-sub notification service with desktop and mobile clients:

`yaml
channels:
  ntfy:
    topic: my-pipeline-alerts
    server: https://ntfy.sh       # Optional, defaults to https://ntfy.sh
    priority: default             # Optional: min, low, default, high, urgent
    events:
      - pipeline_complete
      - pipeline_failed
`

### 2. Discord Webhooks

Send rich embed notifications directly to a Discord text channel:

`yaml
channels:
  discord:
    webhook_url: https://discord.com/api/webhooks/123456789/abcdef...
    events:
      - pipeline_failed
`

### 3. Slack Webhooks

Post formatted updates to your team's Slack channel:

`yaml
channels:
  slack:
    webhook_url: https://hooks.slack.com/services/T00/B00/XXXX
    events:
      - pipeline_failed
      - pipeline_complete
`

### 4. Telegram Bot

Send alerts to Telegram users or groups:

`yaml
channels:
  telegram:
    bot_token: 123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
    chat_id: -1001234567890
    events:
      - pipeline_failed
`

### 5. Custom / Raw Webhooks

Send structured JSON payloads to any HTTP endpoint:

`yaml
channels:
  custom_ci:
    url: https://ci.example.com/api/webhooks/agnview
    method: POST
    headers:
      Authorization: Bearer sec_abc123
    events:
      - pipeline_complete
      - pipeline_failed
`

## Trigger Events

Each channel subscribes via its `events:` list. Use `all` to subscribe to every event.

| Event | Fired when |
| :--- | :--- |
| `task_completed` | An individual task is marked completed |
| `task_failed` | An individual task is marked failed |
| `agent_finished` | An agent process exits (carries `exit_code` and `summary`) |
| `revision_requested` | An agent requests a revision on an upstream task |
| `job_completed` | Every task in a job has completed |
| `pipeline_complete` | Pipeline finished successfully (alias of the job completing) |
| `pipeline_failed` | Pipeline aborted because a task failed |

If a channel omits `events:`, it defaults to `pipeline_failed` and `pipeline_complete`.

## Delivery Reliability

- **Timeout:** each HTTP attempt is given **5 seconds**.
- **Retries:** one initial attempt plus **3 retries**, with **1s / 2s / 4s** backoff between them.
- **Failure visibility:** when all attempts are exhausted, AgnView emits a `system_notice` into the console stream naming the channel, the event and the last error. Delivery failures are never silently discarded.
- Configuration errors (a missing `webhook_url`, `bot_token` or `url`) are reported immediately and are **not** retried.

## Template Placeholders

When AgnView triggers a notification or raw webhook, the following context variables are evaluated:

| Placeholder | Description |
| :--- | :--- |
| {pipeline_id} | Job ID (e.g. job-001) |
| {pipeline_title} | Human-readable title of the pipeline |
| {status} | Final execution status (completed or ailed) |
| {duration} | Total elapsed run duration |
| {failed_task} | Title or identifier of the failed task (on failure) |

## Testing Notifications

- From the Web Dashboard: Open the **Notifications** modal (bell icon in the header) and click **Test Webhook**.
- From REST API: Send a POST request to /api/notifications/test.
