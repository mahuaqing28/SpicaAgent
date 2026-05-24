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
