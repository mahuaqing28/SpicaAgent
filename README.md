# SpicaAgent

SpicaAgent is a small Telegram bridge for Claude Code. It runs Claude in a
background `tmux` session, pastes Telegram messages into the terminal, watches
the pane output, and sends the result back to Telegram.

## Requirements

- Python 3.11+
- `tmux`
- `claude` available on `PATH`
- A Telegram bot token

Install `tmux` on Ubuntu:

```bash
sudo apt update
sudo apt install tmux
```

This machine currently has `claude` available, but `tmux` must be installed
before the bridge can start.

## Configuration

Configuration is read from environment variables. Start by copying
`.env.example` into your shell profile, systemd unit, or another secrets manager.
Do not commit real tokens.

Required:

- `TELEGRAM_BOT_TOKEN`: Telegram Bot API token.
- `TELEGRAM_ALLOWED_CHAT_IDS`: comma-separated allowlist. If this is empty, only
  `/whoami` is usable.

Defaults:

- `TELEGRAM_API_BASE=https://api.telegram.org`
- `TELEGRAM_POLL_TIMEOUT=20`
- `TELEGRAM_DROP_PENDING_UPDATES=true`
- `CLAUDE_WORKDIR=<current directory>`
- `CLAUDE_TMUX_SESSION=claude_bg`
- `CLAUDE_COMMAND=claude`
- `CLAUDE_ENV_FILE=/home/mahuaqing/config/deepseek.txt`
- `CLAUDE_FORWARD_ENV_VARS=HTTP_PROXY,HTTPS_PROXY,ALL_PROXY,NO_PROXY,http_proxy,https_proxy,all_proxy,no_proxy`
- `CLAUDE_READY_TIMEOUT=120`
- `CLAUDE_CAPTURE_LINES=2000`
- `PHONE_BRIDGE_ENABLED=false`
- `PHONE_BRIDGE_HOST=0.0.0.0`
- `PHONE_BRIDGE_PORT=8765`
- `PHONE_BRIDGE_TOKEN=<empty>`
- `PHONE_NOTIFY_CHAT_IDS=<empty, falls back to TELEGRAM_ALLOWED_CHAT_IDS>`
- `SPICA_FILES_ENABLED=false`
- `SPICA_FILE_ROOT=/tmp/spica-agent`
- `SPICA_FILE_OUTPUT_ROOTS=<CLAUDE_WORKDIR>,/tmp/spica-agent/outputs`
- `SPICA_FILE_ALLOWED_EXTENSIONS=.png,.jpg,.jpeg,.webp,.gif,.pdf,.txt,.md,.zip`
- `SPICA_FILE_MAX_UPLOAD_MB=50`
- `SCHEDULE_BRIDGE_ENABLED=false`
- `SCHEDULE_BRIDGE_TOKEN=<PHONE_BRIDGE_TOKEN>`
- `SCHEDULE_STATE_FILE=/tmp/spica-agent/schedule-state.json`
- `SCHEDULE_STATESHARE_FILE=<empty>`
- `SCHEDULE_NON_WORK_PACKAGES=<empty>`
- `SCHEDULE_NON_WORK_THRESHOLD_MINUTES=20`
- `SCHEDULE_REMINDER_COOLDOWN_MINUTES=120`

`CLAUDE_ENV_FILE` is parsed safely. Only `export KEY=value` and `KEY=value`
lines are accepted; the bridge does not execute shell commands from the file.

## Run

```bash
export TELEGRAM_BOT_TOKEN="123456:..."
export TELEGRAM_ALLOWED_CHAT_IDS="123456789"
python3 -m spica_agent
```

First send `/whoami` to the bot from Telegram to discover your `chat_id`, then
add it to `TELEGRAM_ALLOWED_CHAT_IDS` and restart the bridge.

If you are behind a local proxy, export proxy variables before running the
bridge. Use an HTTP or mixed proxy port:

```bash
export HTTPS_PROXY=http://127.0.0.1:7897
export HTTP_PROXY=http://127.0.0.1:7897
export NO_PROXY=localhost,127.0.0.1
```

Some proxies close idle long-polling requests after about 30 seconds. Keep
`TELEGRAM_POLL_TIMEOUT` lower than the proxy idle timeout, for example `20`.
The same proxy variables are forwarded into the Claude tmux session by default,
so Claude Code starts with the proxy environment too.
By default, pending Telegram updates are dropped when the bridge starts. This
prevents old messages from being replayed into Claude after restarts.
If your proxy still closes Telegram long-polling TLS connections, try
`TELEGRAM_POLL_TIMEOUT=3` or `TELEGRAM_POLL_TIMEOUT=0`. Repeated identical
Telegram network warnings are rate-limited in the logs.

## Telegram Commands

- `/whoami`: show the current chat id.
- `/status`: show worker state, active chat, queue length, and tmux session.
- `/phone`: show the most recent Android companion status received by the phone
  bridge.
- `/ask_phone <question>`: ask Claude with the latest Android companion status
  injected from bridge memory.
- `/schedule`: show the latest synced schedule state.
- `/ask_day <question>`: ask Claude with the latest synced schedule state
  injected from bridge memory.
- `/files`: list recent files that can be sent back to Telegram when file
  bridge support is enabled.
- `/file <id>`: send a listed file back as a Telegram document.
- `/photo <id>`: send a listed image file back as a Telegram photo.
- `/last_file`: show the most recent file uploaded by the current chat.
- `/clear_files_context`: stop automatically attaching the current chat's most
  recent uploaded file path to new prompts.
- `/cancel`: send Ctrl-C to the active Claude operation.
- `/restart_claude` or `/new_claude`: restart the background Claude Code tmux
  session and clear Claude's conversation context. This only runs when the
  worker is idle and the queue is empty.
- `/approve` or `y`: approve a Claude `[y/n]` confirmation prompt.
- `/approve_always` or `2`: choose the second Claude Code command-approval
  option, usually "Yes, and don't ask again".
- `/deny` or `n`: deny a Claude confirmation prompt.
- `/up`, `/down`, `/left`, `/right`, `/enter`, `/esc`, `/tab`: send navigation
  keys to Claude Code's current TUI screen.
- `/key d`: send one safe literal key, useful for Claude Code menus like setting
  a selected model as default.

Claude Code slash commands that are not bridge commands, such as `/model`,
`/clear`, or `/compact`, are forwarded into Claude Code like normal input.
For example, send `/model`, then use `/down` and `/enter` to choose a model from
Claude Code's interactive menu. When Claude Code opens a recognized interactive
menu, the bridge sends the current menu text back to Telegram and leaves the menu
open for navigation commands.

## Notes

The bridge processes messages serially because it controls a single Claude Code
terminal. Long Claude responses are split into Telegram-sized messages and
truncated by `TELEGRAM_MAX_REPLY_CHARS` if needed. Before each prompt is sent,
the worker waits until Claude Code is idle, and returned text is filtered to
remove Claude's terminal chrome such as the welcome screen, prompt line, borders,
and shortcut footer. During polling, only the most recent `CLAUDE_CAPTURE_LINES`
tmux lines are captured to keep long-running sessions responsive.

## Telegram File Bridge

Set `SPICA_FILES_ENABLED=true` to let the Telegram bot receive photos and
documents. Uploaded files are downloaded to
`SPICA_FILE_ROOT/uploads/<chat_id>/<date>/`, assigned a short local file id, and
remembered as the current chat's most recent file. If the Telegram message has a
caption, the bridge queues the caption for Claude with the local file path
included. If it has no caption, the next normal text prompt from the same chat
automatically includes that recent file path.

The bridge only lists and sends files whose real paths are under the upload
directory or one of `SPICA_FILE_OUTPUT_ROOTS`, and whose extensions are in
`SPICA_FILE_ALLOWED_EXTENSIONS`. Use `/files` to discover recent files and
`/photo <id>` or `/file <id>` to send generated outputs back to Telegram.

## Schedule Supervision Bridge

Set `SCHEDULE_BRIDGE_ENABLED=true` to accept schedule snapshots and changes from
a mobile schedule app such as `time-is-money-app`. The schedule bridge uses the
same small HTTP server as the Android phone status bridge:

- `POST /api/schedule/snapshot`: replace the current schedule snapshot.
- `POST /api/schedule/changes`: apply changed tasks.
- `GET /api/schedule/status`: return the authenticated full current state for
  mobile clients and debugging.
- `GET /api/schedule/stateshare`: return the authenticated public stateShare
  payload without detailed app package usage.

Requests use `Authorization: Bearer <SCHEDULE_BRIDGE_TOKEN>`. If
`SCHEDULE_BRIDGE_TOKEN` is empty, it falls back to `PHONE_BRIDGE_TOKEN`, which is
convenient when the phone status bridge and schedule bridge are used by the same
device. The payload accepts `device_id`, `today`, `timezone`, `sent_at_ms`,
`tasks` or `changed_tasks`, and an optional `phone_status` object shaped like the
Android companion status snapshot.

The bridge persists schedule state to `SCHEDULE_STATE_FILE`. If
`SCHEDULE_STATESHARE_FILE` is set, it also writes a public stateShare-compatible
`status.json` with schedule titles, completion state, progress, focus, and
energy, but without detailed app package usage. Configure
`SCHEDULE_NON_WORK_PACKAGES` with comma-separated package names such as
`com.instagram.android,com.google.android.youtube`; when one of those apps
exceeds `SCHEDULE_NON_WORK_THRESHOLD_MINUTES` while a high-priority or imminent
task is unfinished, the bridge queues an agent prompt and sends a Telegram
notice. Repeated reminders for the same task/app pair are cooled down by
`SCHEDULE_REMINDER_COOLDOWN_MINUTES`.
See `docs/time-is-money-sync-contract.md` for the exact Android payload contract
and the recommended `time-is-money-app` WorkManager sync points.

## Android Companion Status Bridge

Set `PHONE_BRIDGE_ENABLED=true` and `PHONE_BRIDGE_TOKEN` to start a small local
HTTP receiver for the Android companion app. The app sends batched status events
to `POST /api/phone/events` with `Authorization: Bearer <PHONE_BRIDGE_TOKEN>`.
The receiver stores the latest status in memory, exposes it via `/phone`, and
sends proactive Telegram notifications for first connection, low battery, and
long app usage. If `PHONE_NOTIFY_CHAT_IDS` is empty, proactive messages are sent
to `TELEGRAM_ALLOWED_CHAT_IDS`.
