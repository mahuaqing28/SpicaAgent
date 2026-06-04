from __future__ import annotations

import logging
import os
import time

from .config import AppConfig, ConfigError
from .env_file import EnvFileError, load_env_file
from .files import (
    FileStoreError,
    SpicaFileStore,
    file_context_message,
    format_file_list,
)
from .phone import PhoneStateStore
from .phone_http import PhoneHttpServer
from .schedule import ScheduleReminder, ScheduleStateStore
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
        file_store: SpicaFileStore | None = None,
        schedule_store: ScheduleStateStore | None = None,
    ) -> None:
        self._config = config
        self._telegram = telegram
        self._worker = worker
        self._phone_store = phone_store
        self._file_store = file_store
        self._schedule_store = schedule_store
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

        if message.attachment is not None:
            self._handle_attachment(message)
            return

        if command == "/status":
            self._telegram.send_message(message.chat_id, self._status_text())
            return

        if command in {"/files", "/file", "/photo", "/last_file", "/clear_files_context"}:
            self._handle_file_command(message, command)
            return

        if command == "/schedule":
            if self._schedule_store is None:
                self._telegram.send_message(message.chat_id, "日程接收端未启用。")
            else:
                self._telegram.send_message(
                    message.chat_id,
                    self._schedule_store.format_status(),
                )
            return

        if command == "/ask_day":
            schedule_status = (
                "日程接收端未启用。"
                if self._schedule_store is None
                else self._schedule_store.format_status()
            )
            question = _command_argument(text) or "请根据当前日程和手机状态，判断我现在该做什么。"
            queue_position = self._worker.enqueue(
                WorkItem(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    text=_schedule_context_message(schedule_status, question),
                )
            )
            self._telegram.send_message(
                message.chat_id,
                f"已携带日程状态加入队列，当前位置：{queue_position}",
                reply_to_message_id=message.message_id,
            )
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

        if command == "/ask_phone":
            phone_status = (
                "手机状态接收端未启用。"
                if self._phone_store is None
                else self._phone_store.format_latest_status()
            )
            question = _command_argument(text) or "请根据当前手机状态，给出简要观察和建议。"
            queue_position = self._worker.enqueue(
                WorkItem(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    text=_phone_context_message(phone_status, question),
                )
            )
            self._telegram.send_message(
                message.chat_id,
                f"已携带手机状态加入队列，当前位置：{queue_position}",
                reply_to_message_id=message.message_id,
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
                text=self._claude_text_with_file_context(message),
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

    def _handle_attachment(self, message: TelegramMessage) -> None:
        if self._file_store is None:
            self._telegram.send_message(
                message.chat_id,
                "文件功能未启用。请设置 SPICA_FILES_ENABLED=true 后重启 bridge。",
                reply_to_message_id=message.message_id,
            )
            return
        attachment = message.attachment
        if attachment is None:
            return
        max_bytes = self._config.spica_file_max_upload_mb * 1024 * 1024
        try:
            content = self._telegram.download_file(
                attachment.file_id,
                max_bytes=max_bytes,
            )
            stored = self._file_store.save_upload(
                chat_id=message.chat_id,
                original_name=attachment.file_name,
                source=attachment.kind,
                content=content,
            )
        except (TelegramError, FileStoreError, OSError) as exc:
            LOGGER.warning("Failed to save Telegram attachment: %s", exc)
            self._telegram.send_message(
                message.chat_id,
                f"文件保存失败：{exc}",
                reply_to_message_id=message.message_id,
            )
            return

        lines = [
            "文件已保存。",
            f"id: {stored.id}",
            f"path: {stored.path}",
        ]
        if message.text.strip():
            queue_position = self._worker.enqueue(
                WorkItem(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    text=file_context_message(stored.path, _claude_message_text(message.text)),
                )
            )
            lines.append(f"caption 已携带文件路径加入队列，当前位置：{queue_position}")
        else:
            lines.append("没有 caption，已记录为最近上传文件。可继续发文字指令引用它。")
        self._telegram.send_message(
            message.chat_id,
            "\n".join(lines),
            reply_to_message_id=message.message_id,
        )

    def _handle_file_command(self, message: TelegramMessage, command: str) -> None:
        if self._file_store is None:
            self._telegram.send_message(message.chat_id, "文件功能未启用。")
            return

        if command == "/files":
            self._telegram.send_message(
                message.chat_id,
                format_file_list(self._file_store.list_recent()),
            )
            return

        if command == "/last_file":
            stored = self._file_store.last_for_chat(message.chat_id)
            if stored is None:
                self._telegram.send_message(message.chat_id, "当前 chat 没有最近上传文件。")
            else:
                self._telegram.send_message(
                    message.chat_id,
                    "\n".join(
                        [
                            "最近上传文件：",
                            f"id: {stored.id}",
                            f"name: {stored.name}",
                            f"path: {stored.path}",
                        ]
                    ),
                )
            return

        if command == "/clear_files_context":
            self._file_store.clear_last_for_chat(message.chat_id)
            self._telegram.send_message(message.chat_id, "已清除最近上传文件引用。")
            return

        file_id = _command_argument(message.text)
        if not file_id:
            self._telegram.send_message(message.chat_id, f"用法：{command} <id>")
            return
        stored = self._file_store.get(file_id)
        if stored is None:
            self._telegram.send_message(message.chat_id, "没有找到这个文件，或文件不在白名单目录内。")
            return

        try:
            if command == "/photo":
                if not stored.is_photo:
                    self._telegram.send_message(message.chat_id, "这个文件不是支持的图片类型。")
                    return
                self._telegram.send_photo(
                    message.chat_id,
                    stored.path,
                    caption=f"{stored.name} ({stored.id})",
                    reply_to_message_id=message.message_id,
                )
            else:
                self._telegram.send_document(
                    message.chat_id,
                    stored.path,
                    caption=f"{stored.name} ({stored.id})",
                    reply_to_message_id=message.message_id,
                )
        except TelegramError as exc:
            LOGGER.warning("Failed to send Telegram file: %s", exc)
            self._telegram.send_message(
                message.chat_id,
                f"文件发送失败：{exc}\n本机路径：{stored.path}",
            )

    def _claude_text_with_file_context(self, message: TelegramMessage) -> str:
        text = _claude_message_text(message.text)
        if self._file_store is None:
            return text
        stored = self._file_store.last_for_chat(message.chat_id)
        if stored is None:
            return text
        return file_context_message(stored.path, text)

    def _status_text(self) -> str:
        status = self._worker.status()
        active = status.active_chat_id if status.active_chat_id is not None else "-"
        lines = [
            f"状态: {status.state}",
            f"当前 chat_id: {active}",
            f"队列长度: {status.queue_size}",
            f"tmux session: {self._config.claude_tmux_session}",
        ]
        if self._file_store is None:
            lines.append("文件功能: 未启用")
        else:
            lines.append(self._file_store.status_text())
        lines.append(
            "日程功能: 已启用" if self._schedule_store is not None else "日程功能: 未启用"
        )
        return "\n".join(lines)

    def enqueue_schedule_reminder(self, chat_id: int, reminder: ScheduleReminder) -> None:
        queue_position = self._worker.enqueue(
            WorkItem(
                chat_id=chat_id,
                message_id=0,
                text=reminder.agent_prompt,
            )
        )
        self._telegram.send_message(
            chat_id,
            f"{reminder.text}\n已交给 agent 生成提醒，队列位置：{queue_position}",
        )


def _command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    first = text.split(maxsplit=1)[0].lower()
    return first.split("@", maxsplit=1)[0]


def _command_argument(text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        return ""
    return parts[1].strip()


def _phone_context_message(phone_status: str, question: str) -> str:
    return "\n".join(
        [
            "以下是 SpicaAgent bridge 当前内存中的手机状态：",
            phone_status,
            "",
            "用户问题：",
            question,
        ]
    )


def _schedule_context_message(schedule_status: str, question: str) -> str:
    return "\n".join(
        [
            "以下是 SpicaAgent bridge 当前记录的日程状态：",
            schedule_status,
            "",
            "用户问题：",
            question,
        ]
    )


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
    file_store: SpicaFileStore | None = None
    schedule_store: ScheduleStateStore | None = None
    if config.spica_files_enabled:
        file_store = SpicaFileStore(
            root=config.spica_file_root,
            output_roots=config.spica_file_output_roots,
            allowed_extensions=config.spica_file_allowed_extensions,
            max_upload_bytes=config.spica_file_max_upload_mb * 1024 * 1024,
        )
        LOGGER.info("Spica file bridge enabled at %s", config.spica_file_root)
    if config.schedule_bridge_enabled:
        schedule_store = ScheduleStateStore(
            state_file=config.schedule_state_file,
            state_share_file=config.schedule_stateshare_file,
            non_work_packages=config.schedule_non_work_packages,
            non_work_threshold_minutes=config.schedule_non_work_threshold_minutes,
            reminder_cooldown_minutes=config.schedule_reminder_cooldown_minutes,
        )
        LOGGER.info("Schedule bridge enabled with state file %s", config.schedule_state_file)
    if config.phone_bridge_enabled:
        phone_store = PhoneStateStore()
    if config.phone_bridge_enabled or config.schedule_bridge_enabled:
        notify_chat_ids = config.phone_notify_chat_ids or config.telegram_allowed_chat_ids
        app_ref: BridgeApp | None = None

        def schedule_callback(chat_id: int, reminder: ScheduleReminder) -> None:
            if app_ref is None:
                telegram.send_message(chat_id, reminder.text)
                return
            app_ref.enqueue_schedule_reminder(chat_id, reminder)

        phone_http = PhoneHttpServer(
            host=config.phone_bridge_host,
            port=config.phone_bridge_port,
            token=config.phone_bridge_token or config.schedule_bridge_token,
            store=phone_store,
            telegram=telegram,
            notify_chat_ids=notify_chat_ids,
            schedule_store=schedule_store,
            schedule_token=config.schedule_bridge_token,
            schedule_reminder_callback=schedule_callback,
        )
        phone_http.start()
        host, port = phone_http.server_address
        LOGGER.info("Bridge HTTP started on %s:%s", host, port)
    LOGGER.info("SpicaAgent bridge started")
    app = BridgeApp(config, telegram, worker, phone_store, file_store, schedule_store)
    if "app_ref" in locals():
        app_ref = app
    app.run()
    return 0


def _forwarded_env(config: AppConfig) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in config.claude_forward_env_vars:
        value = os.environ.get(name)
        if value:
            values[name] = value
    return values
