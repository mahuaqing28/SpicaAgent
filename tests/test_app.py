from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spica_agent.app import BridgeApp
from spica_agent.config import AppConfig
from spica_agent.telegram import TelegramMessage
from spica_agent.worker import WorkerStatus


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.update_batches: list[list[dict]] = []
        self.get_updates_calls: list[tuple[int | None, int]] = []

    def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))

    def get_updates(self, *, offset: int | None, timeout: int):
        self.get_updates_calls.append((offset, timeout))
        if self.update_batches:
            return self.update_batches.pop(0)
        return []


class FakeWorker:
    def __init__(self) -> None:
        self.items = []
        self.confirmed = False
        self.cancelled = False
        self.keys = []

    def provide_confirmation(self, chat_id: int, text: str) -> bool:
        if self.confirmed:
            return True
        return False

    def enqueue(self, item):
        self.items.append(item)
        return len(self.items)

    def status(self):
        return WorkerStatus(state="idle", active_chat_id=None, queue_size=len(self.items))

    def cancel(self, chat_id: int) -> bool:
        self.cancelled = True
        return True

    def restart_claude(self):
        return True, "restarted"

    def send_tui_key(self, key: str):
        self.keys.append(key)
        return True, f"key {key}"


class AppTests(unittest.TestCase):
    def make_config(self, allowed: str) -> AppConfig:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            env_file = path / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            return AppConfig.from_env(
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "TELEGRAM_ALLOWED_CHAT_IDS": allowed,
                    "CLAUDE_WORKDIR": str(path),
                    "CLAUDE_ENV_FILE": str(env_file),
                },
                cwd=path,
            )

    def test_whoami_allowed_without_allowlist(self) -> None:
        telegram = FakeTelegram()
        app = BridgeApp(self.make_config(""), telegram, FakeWorker())

        app.handle_message(TelegramMessage(1, 42, 7, "/whoami"))

        self.assertEqual(telegram.sent[-1], (42, "chat_id: 42"))

    def test_rejects_non_allowlisted_chat(self) -> None:
        telegram = FakeTelegram()
        app = BridgeApp(self.make_config("1"), telegram, FakeWorker())

        app.handle_message(TelegramMessage(1, 42, 7, "hello"))

        self.assertIn("未授权", telegram.sent[-1][1])

    def test_enqueues_allowlisted_message(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        app = BridgeApp(self.make_config("42"), telegram, worker)

        app.handle_message(TelegramMessage(1, 42, 7, "hello"))

        self.assertEqual(worker.items[0].text, "hello")
        self.assertIn("已加入队列", telegram.sent[-1][1])

    def test_forwards_unknown_slash_commands_to_claude(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        app = BridgeApp(self.make_config("42"), telegram, worker)

        app.handle_message(TelegramMessage(1, 42, 7, "/model@Spica_2049Bot deepseek"))

        self.assertEqual(worker.items[0].text, "/model deepseek")

    def test_restart_claude_command_is_handled_by_bridge(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        app = BridgeApp(self.make_config("42"), telegram, worker)

        app.handle_message(TelegramMessage(1, 42, 7, "/restart_claude"))

        self.assertEqual(telegram.sent[-1], (42, "restarted"))
        self.assertEqual(worker.items, [])

    def test_tui_navigation_command_sends_key_without_queueing(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        app = BridgeApp(self.make_config("42"), telegram, worker)

        app.handle_message(TelegramMessage(1, 42, 7, "/down"))

        self.assertEqual(worker.keys, ["Down"])
        self.assertEqual(worker.items, [])

    def test_key_command_sends_safe_literal_key(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        app = BridgeApp(self.make_config("42"), telegram, worker)

        app.handle_message(TelegramMessage(1, 42, 7, "/key d"))

        self.assertEqual(worker.keys, ["d"])

    def test_initial_offset_drops_pending_updates(self) -> None:
        telegram = FakeTelegram()
        telegram.update_batches = [
            [{"update_id": 10}, {"update_id": 11}],
            [],
        ]
        app = BridgeApp(self.make_config("42"), telegram, FakeWorker())

        offset = app._initial_offset()

        self.assertEqual(offset, 12)
        self.assertEqual(telegram.get_updates_calls, [(None, 0), (12, 0)])


if __name__ == "__main__":
    unittest.main()
