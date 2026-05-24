from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

from .config import AppConfig
from .telegram import TelegramClient
from .text_utils import split_telegram_text, strip_claude_tui_chrome
from .tmux_bridge import TmuxBridge
from .waiter import ReplyEvent, ReplyWaiter, contains_command_approval


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkItem:
    chat_id: int
    message_id: int
    text: str


@dataclass(frozen=True)
class WorkerStatus:
    state: str
    active_chat_id: int | None
    queue_size: int


class ClaudeWorker:
    def __init__(
        self,
        config: AppConfig,
        tmux: TmuxBridge,
        telegram: TelegramClient,
    ) -> None:
        self._config = config
        self._tmux = tmux
        self._telegram = telegram
        self._queue: queue.Queue[WorkItem] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="claude-worker", daemon=True)
        self._condition = threading.Condition()
        self._state = "idle"
        self._active_chat_id: int | None = None
        self._confirmation: str | None = None

    def start(self) -> None:
        self._thread.start()

    def enqueue(self, item: WorkItem) -> int:
        self._queue.put(item)
        return self._queue.qsize()

    def status(self) -> WorkerStatus:
        with self._condition:
            return WorkerStatus(
                state=self._state,
                active_chat_id=self._active_chat_id,
                queue_size=self._queue.qsize(),
            )

    def provide_confirmation(self, chat_id: int, text: str) -> bool:
        answer = _normalize_confirmation(text)
        if answer is None:
            return False
        with self._condition:
            if self._state != "waiting_confirmation" or self._active_chat_id != chat_id:
                return False
            self._confirmation = answer
            self._condition.notify_all()
            return True

    def cancel(self, chat_id: int) -> bool:
        with self._condition:
            active = self._active_chat_id == chat_id and self._state in {
                "running",
                "waiting_confirmation",
            }
            if self._state == "waiting_confirmation" and self._active_chat_id == chat_id:
                self._confirmation = "cancel"
                self._condition.notify_all()
        if active:
            self._tmux.send_ctrl_c()
        return active

    def restart_claude(self) -> tuple[bool, str]:
        with self._condition:
            if self._state != "idle" or not self._queue.empty():
                return False, "当前 Claude 正在处理任务或队列不为空，请先 /cancel 或等待完成。"
            self._state = "restarting"

        try:
            self._tmux.restart_session()
        except Exception:
            LOGGER.exception("Failed to restart Claude tmux session")
            return False, "重启 Claude 会话失败，请查看服务日志。"
        finally:
            with self._condition:
                self._state = "idle"
                self._active_chat_id = None
                self._confirmation = None

        return True, "已重启 Claude Code，会话上下文已清空。"

    def send_tui_key(self, key: str) -> tuple[bool, str]:
        try:
            self._tmux.send_key(key)
        except Exception:
            LOGGER.exception("Failed to send TUI key to Claude")
            return False, "发送按键失败，请查看服务日志。"
        return True, f"已发送按键：{key}"

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                self._process(item)
            except Exception:
                LOGGER.exception("Failed to process Telegram work item")
                self._safe_send(item.chat_id, "处理失败：后台 bridge 出错，请查看服务日志。")
            finally:
                with self._condition:
                    self._state = "idle"
                    self._active_chat_id = None
                    self._confirmation = None
                self._queue.task_done()

    def _process(self, item: WorkItem) -> None:
        with self._condition:
            self._state = "running"
            self._active_chat_id = item.chat_id
            self._confirmation = None

        waiter = ReplyWaiter(
            self._tmux.capture_screen,
            poll_interval=self._config.claude_poll_interval,
        )
        ready = waiter.wait_until_ready(self._config.claude_ready_timeout)
        if ready.kind != "ready":
            self._safe_send(
                item.chat_id,
                "Claude 尚未进入可输入状态，请稍后重试或检查 tmux 里的 Claude Code。",
            )
            return

        self._tmux.clear_input()
        baseline = self._tmux.capture_screen()
        self._tmux.send_text(item.text)
        self._safe_send(item.chat_id, "指令已发送，正在等待 Claude 回复...")

        while True:
            waiter = ReplyWaiter(
                self._tmux.capture_screen,
                poll_interval=self._config.claude_poll_interval,
            )
            event = waiter.wait(baseline, self._config.claude_reply_timeout)
            if event.kind == "confirmation":
                baseline = self._handle_confirmation(item.chat_id, event)
                if baseline is None:
                    return
                continue

            if event.kind == "interactive":
                output = strip_claude_tui_chrome(event.text.strip())
                self._send_output(
                    item.chat_id,
                    (
                        "Claude Code 进入交互菜单。可继续发送 /up、/down、/enter、/esc "
                        "或 /key d 操作当前界面。\n\n"
                        + (output or strip_claude_tui_chrome(event.screen.strip()))
                    ),
                )
                return

            if event.kind == "done":
                output = strip_claude_tui_chrome(event.text.strip())
                self._send_output(
                    item.chat_id,
                    output or "Claude 已返回输入状态，但没有检测到新增文字回复。",
                )
                return

            self._send_timeout(item.chat_id, event)
            return

    def _handle_confirmation(self, chat_id: int, event: ReplyEvent) -> str | None:
        text = strip_claude_tui_chrome(event.text.strip())
        prompt = event.prompt.strip()
        is_command_approval = contains_command_approval(event.text) or contains_command_approval(
            event.screen
        )
        if is_command_approval:
            message = (
                "Claude 需要确认命令执行。\n"
                "回复 /approve 或 1：执行一次\n"
                "回复 /approve_always 或 2：执行且以后不再询问同类命令\n"
                "回复 /deny 或 3：拒绝"
            )
        else:
            message = "Claude 需要确认，请回复 y/n、/approve 或 /deny。"
        if text:
            message += "\n\n" + text
        elif prompt:
            message += "\n\n" + prompt
        self._send_output(chat_id, message)

        with self._condition:
            self._state = "waiting_confirmation"
            self._confirmation = None
            confirmed = self._condition.wait_for(
                lambda: self._confirmation is not None,
                timeout=self._config.claude_confirm_timeout,
            )
            answer = self._confirmation
            self._state = "running"
            self._confirmation = None

        if not confirmed or answer is None:
            if is_command_approval:
                self._safe_send(chat_id, "确认等待超时，已自动取消审批。")
                self._tmux.send_key("Escape")
            else:
                self._safe_send(chat_id, "确认等待超时，已自动发送 n。")
                self._tmux.send_text("n")
            return event.screen

        if answer == "cancel":
            self._safe_send(chat_id, "已取消当前 Claude 操作。")
            return event.screen

        if is_command_approval:
            self._send_command_approval(answer)
            self._safe_send(chat_id, f"已发送审批选择：{_approval_label(answer)}")
        else:
            injected = "y" if answer in {"approve", "approve_always"} else "n"
            self._safe_send(chat_id, f"已发送确认：{injected}")
            self._tmux.send_text(injected)
        return event.screen

    def _send_command_approval(self, answer: str) -> None:
        if answer == "approve":
            self._tmux.send_key("Enter")
        elif answer == "approve_always":
            self._tmux.send_keys("Down", "Enter")
        else:
            self._tmux.send_key("Escape")

    def _send_timeout(self, chat_id: int, event: ReplyEvent) -> None:
        text = strip_claude_tui_chrome(event.text.strip())
        if text:
            self._send_output(chat_id, text)
        self._safe_send(chat_id, "超时：Claude 思考太久，或终端没有回到输入提示符。")

    def _send_output(self, chat_id: int, text: str) -> None:
        for chunk in split_telegram_text(
            text,
            limit=self._config.telegram_message_limit,
            max_total_chars=self._config.telegram_max_reply_chars,
        ):
            self._safe_send(chat_id, chunk)

    def _safe_send(self, chat_id: int, text: str) -> None:
        try:
            self._telegram.send_message(chat_id, text)
        except Exception:
            LOGGER.exception("Failed to send Telegram message")


def _normalize_confirmation(text: str) -> str | None:
    normalized = text.strip().lower()
    command = normalized.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
    if normalized in {"y", "yes", "1"} or command == "/approve":
        return "approve"
    if normalized in {"2"} or command == "/approve_always":
        return "approve_always"
    if normalized in {"n", "no", "3"} or command == "/deny":
        return "deny"
    return None


def _approval_label(answer: str) -> str:
    if answer == "approve":
        return "执行一次"
    if answer == "approve_always":
        return "执行且以后不再询问同类命令"
    return "拒绝"
