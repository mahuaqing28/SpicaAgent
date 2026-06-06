# time-is-money-app Schedule Sync Contract

This document is the implementation contract for wiring
`/home/mahuaqing/personnalProject/time-is-money-app` into SpicaAgent's cloud
schedule supervision bridge.

## Bridge Configuration

Enable the cloud schedule receiver on the SpicaAgent server:

```bash
export SCHEDULE_BRIDGE_ENABLED=true
export SCHEDULE_BRIDGE_TOKEN="shared-secret"
export PHONE_BRIDGE_HOST=127.0.0.1
export PHONE_BRIDGE_PORT=8765
export SCHEDULE_STATE_FILE=/tmp/spica-agent/schedule-state.json
export SCHEDULE_STATESHARE_FILE=/home/mahuaqing/personnalProject/stateShare/data/status.json
export SCHEDULE_AGENT_DIR=/home/mahuaqing/personnalProject/SpicaAgent/schedule
export SCHEDULE_AGENT_HISTORY_DAYS=7
export SCHEDULE_NON_WORK_PACKAGES="com.google.android.youtube,com.instagram.android"
export SCHEDULE_REMINDER_CHECK_INTERVAL_SECONDS=60
```

If phone status sync and schedule sync come from the same Android device,
`SCHEDULE_BRIDGE_TOKEN` may be omitted and `PHONE_BRIDGE_TOKEN` will be reused.

The bridge writes agent-readable context files to `SCHEDULE_AGENT_DIR`:
`current.json`, `tasks.json`, `today.md`, and `daily/YYYY-MM-DD.json`. The daily
JSON files are pruned to `SCHEDULE_AGENT_HISTORY_DAYS` days. Agent prompts only
carry the trigger reason and these paths, so Claude can inspect the current
files when it needs full task/schedule context.

## Android Client Configuration

`time-is-money-app` should store these values in private app preferences:

- `spica_schedule_bridge_url`: base URL, for example `https://spica.example.com`.
- `spica_schedule_token`: bearer token.
- `spica_schedule_sync_enabled`: boolean.
- `spica_schedule_last_sync_ms`: last successful sync timestamp.

The client should expose a small settings card in the task area with URL, token,
enabled switch, and a manual "sync now" action.

## Snapshot Endpoint

`POST /api/schedule/snapshot`

Use this for app startup, manual sync, daily sync, periodic WorkManager sync, and
boot/package-replaced recovery.

Headers:

```text
Authorization: Bearer <token>
Content-Type: application/json
```

Payload:

```json
{
  "device_id": "android-id-or-model",
  "today": "2026-06-05",
  "timezone": "Asia/Shanghai",
  "sent_at_ms": 1780588800000,
  "tasks": [
    {
      "id": 12,
      "title": "写项目报告",
      "description": "整理实现进度",
      "deadline_ms": 1780603200000,
      "is_completed": false,
      "completed_at_ms": null,
      "parent_id": null,
      "created_at_ms": 1780580000000,
      "priority": 5
    }
  ],
  "schedules": [
    {
      "id": 31,
      "task_id": 12,
      "date": "2026-06-05",
      "type": "TIME_BLOCK",
      "start_time_ms": 1780599600000,
      "end_time_ms": 1780603200000,
      "reminder_enabled": true,
      "reminder_minutes_before": 10
    }
  ],
  "phone_status": {
    "battery_percent": 88,
    "is_charging": false,
    "network_type": "wifi",
    "foreground_app": {
      "package_name": "com.google.android.youtube",
      "app_name": "YouTube"
    },
    "recent_apps": [
      {
        "package_name": "com.google.android.youtube",
        "app_name": "YouTube",
        "total_time_ms": 1500000,
        "last_time_used_ms": 1780588700000
      }
    ]
  }
}
```

The bridge expects the new split payload only. Send task data in `tasks` and
schedule/reminder data in `schedules`. Both arrays are required and may be
empty.

For each task, send:

- `id`
- `title`
- `description`
- `deadline_ms`
- `is_completed`
- `completed_at_ms`
- `parent_id`
- `created_at_ms`
- `priority`

For each schedule, send:

- `id`
- `task_id`
- `date`
- `type`
- `start_time_ms`
- `end_time_ms`
- `reminder_enabled`
- `reminder_minutes_before`

`schedule.task_id` must reference a task included in the same snapshot or
already known to the server during a changes sync.

Success response:

```json
{
  "ok": true,
  "accepted_task_ids": ["12"],
  "accepted_schedule_ids": ["31"],
  "reminder_count": 0
}
```

## Changes Endpoint

`POST /api/schedule/changes`

Use this immediately after `addTask`, `updateTask`, `toggleCompletion`, and
`deleteTask`-adjacent state changes where the task still exists.

Payload:

```json
{
  "device_id": "android-id-or-model",
  "today": "2026-06-05",
  "timezone": "Asia/Shanghai",
  "sent_at_ms": 1780588800000,
  "changed_tasks": [
    {
      "id": 12,
      "title": "写项目报告",
      "description": "整理实现进度",
      "deadline_ms": 1780603200000,
      "is_completed": true,
      "completed_at_ms": 1780590000000,
      "parent_id": null,
      "created_at_ms": 1780580000000,
      "priority": 5
    }
  ],
  "changed_schedules": [
    {
      "id": 31,
      "task_id": 12,
      "date": "2026-06-05",
      "type": "TIME_BLOCK",
      "start_time_ms": 1780599600000,
      "end_time_ms": 1780603200000,
      "reminder_enabled": true,
      "reminder_minutes_before": 10
    }
  ],
  "phone_status": {
    "recent_apps": []
  }
}
```

For deletes, the v1 bridge should send a full snapshot after deletion instead of
inventing a tombstone format.

If only a task changes, send it in `changed_tasks` and send
`"changed_schedules": []`. Existing server-side schedules linked to that task
will refresh their title, description, deadline, completion state, and priority
from the changed task.

## Realtime Query Endpoints

`GET /api/schedule/status`

Returns the authenticated full current bridge state for mobile clients and
debugging. This response includes task details, progress, `phone_status`, and a
human-readable `summary`.

`GET /api/schedule/stateshare`

Returns the authenticated public stateShare-compatible payload. It includes
schedule titles, completion state, progress, focus, and energy, but omits
detailed phone app package usage.

Both endpoints use the same bearer token as the write endpoints.

## Scheduling Rules

The Android client should enqueue:

- One periodic WorkManager sync every 15 minutes when enabled.
- One immediate one-time sync after task create/update/toggle completion.
- One sync on boot/package replaced after existing reminder recovery.
- One manual sync from the settings card.

Use `NetworkType.CONNECTED`; failed syncs should return `Result.retry()` for
workers and show a short UI status for manual sync.

The server also checks stored schedules every
`SCHEDULE_REMINDER_CHECK_INTERVAL_SECONDS` seconds, so configured schedule
reminders can fire even when the phone is not syncing at that exact moment.
Phone status events posted to `/api/phone/events` are also merged into the
schedule store, so `time-is-money-app` does not need to duplicate UsageStats
collection for non-work app detection.

## Acceptance Checklist

- Manual sync posts all current tasks and receives `ok=true`.
- Adding a task posts either `/changes` or a follow-up `/snapshot`.
- Toggling completion posts `/changes`.
- Periodic sync refreshes Spica's `/schedule` output.
- If a configured non-work app exceeds the threshold while a risky task is open,
  Spica sends a Telegram notice and queues an agent prompt.
- Agent-readable files under `SCHEDULE_AGENT_DIR` refresh after each full sync
  and contain the current task database plus the retained daily schedule files.
- `stateShare/data/status.json` is updated without exposing package names.
