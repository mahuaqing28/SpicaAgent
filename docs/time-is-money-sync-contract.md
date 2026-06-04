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
export SCHEDULE_NON_WORK_PACKAGES="com.google.android.youtube,com.instagram.android"
```

If phone status sync and schedule sync come from the same Android device,
`SCHEDULE_BRIDGE_TOKEN` may be omitted and `PHONE_BRIDGE_TOKEN` will be reused.

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
      "protocol": "DUTY",
      "deadline": 1780603200000,
      "isCompleted": false,
      "completedAt": null,
      "parentId": null,
      "createdAt": 1780580000000,
      "startedAt": 1780583600000,
      "priority": 5,
      "reminderEnabled": true,
      "reminderMinutesBefore": 10
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

The bridge accepts both snake_case and current `time-is-money-app` camelCase
field names. For the existing Room `Task` model, send:

- `id`
- `title`
- `description`
- `protocol`
- `deadline`
- `isCompleted`
- `completedAt`
- `parentId`
- `createdAt`
- `startedAt`
- `priority`
- `reminderEnabled`
- `reminderMinutesBefore`

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
      "deadline": 1780603200000,
      "isCompleted": true,
      "completedAt": 1780590000000,
      "priority": 5
    }
  ],
  "phone_status": {
    "recent_apps": []
  }
}
```

For deletes, the v1 bridge should send a full snapshot after deletion instead of
inventing a tombstone format.

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

## Acceptance Checklist

- Manual sync posts all current tasks and receives `ok=true`.
- Adding a task posts either `/changes` or a follow-up `/snapshot`.
- Toggling completion posts `/changes`.
- Periodic sync refreshes Spica's `/schedule` output.
- If a configured non-work app exceeds the threshold while a risky task is open,
  Spica sends a Telegram notice and queues an agent prompt.
- `stateShare/data/status.json` is updated without exposing package names.
