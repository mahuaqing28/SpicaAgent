from __future__ import annotations

import logging
import os
import time

from .config import AppConfig, ConfigError
from .env_file import EnvFileError, load_env_file
from .phone import PhoneStateStore
from .phone_http import PhoneHttpServer
from .telegram import TelegramClient, TelegramError, TelegramMessage
from .tmux_bridge import TmuxBridge, TmuxError
from .worker import ClaudeWorker, WorkItem


LOGGER = logging.getLogger(__name__)


class BridgeApp:
    def __init__(
        self,
        config: AppConfig,
        telegram: TelegramClient,
        worker: ClaudeWorker,
        phone_store: PhoneStateStore | None = None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._worker = worker
        self._phone_store = phone_store
        self._last_telegram_error = ""
        self._last_telegram_error_log_at = 0.0
        self._telegram_error_count = 0

    def run(self) -> None:
        offset: int | None = self._initial_offset()
        while True:
            try:
                updates = self._telegram.get_updates(
                    offset=offset,
                    timeout=self._config.telegram_poll_timeout,
                )
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    message = self._telegram.parse_message(update)
                    if message is not None:
                        self.handle_message(message)
                self._reset_telegram_error_state()
            except KeyboardInterrupt:
                raise
            except TelegramError as exc:
                self._log_telegram_error(exc)
                time.sleep(5)
            except Exception:
                LOGGER.exception("Unexpected polling failure")
                time.sleep(5)

    def _initial_offset(self) -> int | None:
        if not self._config.telegram_drop_pending_updates:
            return None

        offset: int | None = None
        dropped = 0
        while True:
            updates = self._telegram.get_updates(offset=offset, timeout=0)
            if not updates:
                if dropped:
                    LOGGER.info("Dropped %s pending Telegram update(s) on startup", dropped)
                return offset

            max_update_id = max(
                update_id
                for update in updates
                if isinstance((update_id := update.get("update_id")), int)
            )
            offset = max_update_id + 1
            dropped += len(updates)

    def _log_telegram_error(self, exc: TelegramError) -> None:
        message = str(exc)
        now = time.monotonic()
        if message == self._last_telegram_error:
            self._telegram_error_count += 1
        else:
            self._last_telegram_error = message
            self._telegram_error_count = 1
            self._last_telegram_error_log_at = 0.0

        if now - self._last_telegram_error_log_at >= 60:
            suffix = ""
            if self._telegram_error_count > 1:
                suffix = f" (repeated {self._telegram_error_count} times)"
            LOGGER.warning("%s%s", message, suffix)
            self._last_telegram_error_log_at = now

    def _reset_telegram_error_state(self) -> None:
        if self._telegram_error_count:
            LOGGER.info("Telegram polling recovered")
        self._last_telegram_error = ""
        self._last_telegram_error_log_at = 0.0
        self._telegram_error_count = 0

    def handle_message(self, message: TelegramMessage) -> None:
        text = message.text.strip()
        command = _command_name(text)

        if command == "/whoami":
            self._telegram.send_message(
                message.chat_id,
                f"chat_id: {message.chat_id}",
                reply_to_message_id=message.message_id,
            )
            return

        if not self._is_allowed(message.chat_id):
            self._telegram.send_message(
                message.chat_id,
                "未授权：请把这个 chat_id 加入 TELEGRAM_ALLOWED_CHAT_IDS。先发送 /whoami 查看 chat_id。",
                reply_to_message_id=message.message_id,
            )
            return

        if self._worker.provide_confirmation(message.chat_id, text):
            self._telegram.send_message(message.chat_id, "确认已收到。")
            return

        if command == "/status":
            self._telegram.send_message(message.chat_id, self._status_text())
            return

        if command == "/phone":
            if self._phone_store is None:
                self._telegram.send_message(message.chat_id, "手机状态接收端未启用。")
            else:
                self._telegram.send_message(
                    message.chat_id,
                    self._phone_store.format_latest_status(),
                )
            return

        if command == "/cancel":
            if self._worker.cancel(message.chat_id):
                self._telegram.send_message(message.chat_id, "已向 Claude 发送 Ctrl-C。")
            else:
                self._telegram.send_message(message.chat_id, "当前没有可取消的 Claude 操作。")
            return

        if command in {"/restart_claude", "/new_claude"}:
            ok, response = self._worker.restart_claude()
            self._telegram.send_message(message.chat_id, response)
            return

        key = _tui_key_for_command(command, text)
        if key is not None:
            ok, response = self._worker.send_tui_key(key)
            self._telegram.send_message(message.chat_id, response)
            return

        if command in {"/approve", "/approve_always", "/deny"}:
            self._telegram.send_message(message.chat_id, "当前没有等待确认的 Claude 操作。")
            return

        if not text:
            return

        queue_position = self._worker.enqueue(
            WorkItem(
                chat_id=message.chat_id,
                message_id=message.message_id,
                text=_claude_message_text(message.text),
            )
        )
        self._telegram.send_message(
            message.chat_id,
            f"已加入队列，当前位置：{queue_position}",
            reply_to_message_id=message.message_id,
        )

    def _is_allowed(self, chat_id: int) -> bool:
        allowed = self._config.telegram_allowed_chat_ids
        return bool(allowed) and chat_id in allowed

    def _status_text(self) -> str:
        status = self._worker.status()
        active = status.active_chat_id if status.active_chat_id is not None else "-"
        return "\n".join(
            [
                f"状态: {status.state}",
                f"当前 chat_id: {active}",
                f"队列长度: {status.queue_size}",
                f"tmux session: {self._config.claude_tmux_session}",
            ]
        )


def _command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    first = text.split(maxsplit=1)[0].lower()
    return first.split("@", maxsplit=1)[0]


def _claude_message_text(text: str) -> str:
    if not text.startswith("/"):
        return text
    parts = text.split(maxsplit=1)
    command = parts[0]
    if "@" not in command:
        return text
    normalized = command.split("@", maxsplit=1)[0]
    if len(parts) == 1:
        return normalized
    return normalized + " " + parts[1]


def _tui_key_for_command(command: str | None, text: str) -> str | None:
    simple_keys = {
        "/up": "Up",
        "/down": "Down",
        "/left": "Left",
        "/right": "Right",
        "/enter": "Enter",
        "/esc": "Escape",
        "/escape": "Escape",
        "/tab": "Tab",
    }
    if command in simple_keys:
        return simple_keys[command]
    if command != "/key":
        return None

    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        return None
    return _safe_tui_key(parts[1].strip())


def _safe_tui_key(raw_key: str) -> str | None:
    aliases = {
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "enter": "Enter",
        "return": "Enter",
        "esc": "Escape",
        "escape": "Escape",
        "tab": "Tab",
        "space": "Space",
        "backspace": "BSpace",
        "delete": "Delete",
        "ctrl-c": "C-c",
        "ctrl-u": "C-u",
    }
    lowered = raw_key.lower()
    if lowered in aliases:
        return aliases[lowered]
    if len(raw_key) == 1 and raw_key.isprintable():
        return raw_key
    return None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = AppConfig.from_env()
        claude_env = load_env_file(config.claude_env_file)
        claude_env.update(_forwarded_env(config))
        telegram = TelegramClient(
            config.telegram_bot_token,
            api_base=config.telegram_api_base,
        )
        tmux = TmuxBridge(config, claude_env)
        tmux.ensure_session()
    except (ConfigError, EnvFileError, TmuxError) as exc:
        LOGGER.error("%s", exc)
        return 2

    worker = ClaudeWorker(config, tmux, telegram)
    worker.start()
    phone_store: PhoneStateStore | None = None
    if config.phone_bridge_enabled:
        phone_store = PhoneStateStore()
        notify_chat_ids = config.phone_notify_chat_ids or config.telegram_allowed_chat_ids
        phone_http = PhoneHttpServer(
            host=config.phone_bridge_host,
            port=config.phone_bridge_port,
            token=config.phone_bridge_token,
            store=phone_store,
            telegram=telegram,
            notify_chat_ids=notify_chat_ids,
        )
        phone_http.start()
        host, port = phone_http.server_address
        LOGGER.info("Phone bridge HTTP started on %s:%s", host, port)
    LOGGER.info("SpicaAgent bridge started")
    BridgeApp(config, telegram, worker, phone_store).run()
    return 0


def _forwarded_env(config: AppConfig) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in config.claude_forward_env_vars:
        value = os.environ.get(name)
        if value:
            values[name] = value
    return values
