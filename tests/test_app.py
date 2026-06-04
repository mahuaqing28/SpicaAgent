from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spica_agent.app import BridgeApp
from spica_agent.config import AppConfig
from spica_agent.files import SpicaFileStore
from spica_agent.phone import PhoneStateStore
from spica_agent.schedule import ScheduleStateStore
from spica_agent.telegram import TelegramAttachment, TelegramMessage
from spica_agent.worker import WorkerStatus


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.update_batches: list[list[dict]] = []
        self.get_updates_calls: list[tuple[int | None, int]] = []
        self.downloads: dict[str, bytes] = {}
        self.sent_photos: list[tuple[int, Path]] = []
        self.sent_documents: list[tuple[int, Path]] = []

    def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))

    def get_updates(self, *, offset: int | None, timeout: int):
        self.get_updates_calls.append((offset, timeout))
        if self.update_batches:
            return self.update_batches.pop(0)
        return []

    def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        return self.downloads[file_id]

    def send_photo(self, chat_id: int, path: Path, **kwargs) -> None:
        self.sent_photos.append((chat_id, path))

    def send_document(self, chat_id: int, path: Path, **kwargs) -> None:
        self.sent_documents.append((chat_id, path))


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
    def make_config(
        self,
        allowed: str,
        *,
        file_root: Path | None = None,
        output_root: Path | None = None,
        files_enabled: bool = False,
    ) -> AppConfig:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            env_file = path / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            env = {
                "TELEGRAM_BOT_TOKEN": "token",
                "TELEGRAM_ALLOWED_CHAT_IDS": allowed,
                "CLAUDE_WORKDIR": str(path),
                "CLAUDE_ENV_FILE": str(env_file),
                "SPICA_FILES_ENABLED": "true" if files_enabled else "false",
            }
            if file_root is not None:
                env["SPICA_FILE_ROOT"] = str(file_root)
            if output_root is not None:
                env["SPICA_FILE_OUTPUT_ROOTS"] = str(output_root)
            return AppConfig.from_env(env, cwd=path)

    def make_file_store(self, root: Path, output: Path) -> SpicaFileStore:
        return SpicaFileStore(
            root=root,
            output_roots=(output,),
            allowed_extensions=frozenset({".png", ".jpg", ".txt", ".zip"}),
            max_upload_bytes=1024 * 1024,
            now=lambda: 1_700_000_000,
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

    def test_phone_command_reports_disabled_when_no_store(self) -> None:
        telegram = FakeTelegram()
        app = BridgeApp(self.make_config("42"), telegram, FakeWorker())

        app.handle_message(TelegramMessage(1, 42, 7, "/phone"))

        self.assertIn("未启用", telegram.sent[-1][1])

    def test_phone_command_reports_latest_status(self) -> None:
        telegram = FakeTelegram()
        store = PhoneStateStore()
        store.process_payload(
            {
                "device_id": "device-1",
                "events": [
                    {
                        "event_id": "event-1",
                        "occurred_at_ms": 1_700_000_000_000,
                        "collected_at_ms": 1_700_000_000_000,
                        "type": "status",
                        "snapshot": {
                            "manufacturer": "Google",
                            "model": "Pixel",
                            "battery_percent": 88,
                            "is_charging": True,
                            "network_type": "wifi",
                            "usage_access_granted": True,
                        },
                    }
                ],
            },
            now_ms=1_700_000_000_000,
        )
        app = BridgeApp(self.make_config("42"), telegram, FakeWorker(), store)

        app.handle_message(TelegramMessage(1, 42, 7, "/phone"))

        self.assertIn("Google Pixel", telegram.sent[-1][1])
        self.assertIn("88%", telegram.sent[-1][1])

    def test_ask_phone_enqueues_prompt_with_latest_status(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        store = PhoneStateStore()
        store.process_payload(
            {
                "device_id": "device-1",
                "events": [
                    {
                        "event_id": "event-1",
                        "occurred_at_ms": 1_700_000_000_000,
                        "collected_at_ms": 1_700_000_000_000,
                        "type": "status",
                        "snapshot": {
                            "manufacturer": "Google",
                            "model": "Pixel",
                            "battery_percent": 88,
                            "is_charging": True,
                            "network_type": "wifi",
                            "usage_access_granted": True,
                        },
                    }
                ],
            },
            now_ms=1_700_000_000_000,
        )
        app = BridgeApp(self.make_config("42"), telegram, worker, store)

        app.handle_message(TelegramMessage(1, 42, 7, "/ask_phone 我现在适合继续工作吗？"))

        self.assertIn("已携带手机状态加入队列", telegram.sent[-1][1])
        self.assertIn("Google Pixel", worker.items[0].text)
        self.assertIn("我现在适合继续工作吗？", worker.items[0].text)

    def test_ask_phone_enqueues_disabled_status_when_no_store(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        app = BridgeApp(self.make_config("42"), telegram, worker)

        app.handle_message(TelegramMessage(1, 42, 7, "/ask_phone"))

        self.assertIn("手机状态接收端未启用", worker.items[0].text)
        self.assertIn("简要观察和建议", worker.items[0].text)

    def test_schedule_command_reports_latest_schedule_status(self) -> None:
        telegram = FakeTelegram()
        schedule_store = ScheduleStateStore()
        schedule_store.process_snapshot(
            {
                "today": "2026-06-04",
                "sent_at_ms": 1_700_000_000_000,
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "写项目报告",
                        "deadline_ms": 1_700_003_600_000,
                        "priority": 5,
                    }
                ],
            },
            now_ms=1_700_000_000_000,
        )
        app = BridgeApp(
            self.make_config("42"),
            telegram,
            FakeWorker(),
            schedule_store=schedule_store,
        )

        app.handle_message(TelegramMessage(1, 42, 7, "/schedule"))

        self.assertIn("当前日程状态", telegram.sent[-1][1])
        self.assertIn("写项目报告", telegram.sent[-1][1])

    def test_ask_day_enqueues_prompt_with_schedule_status(self) -> None:
        telegram = FakeTelegram()
        worker = FakeWorker()
        schedule_store = ScheduleStateStore()
        schedule_store.process_snapshot(
            {
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "写项目报告",
                        "deadline_ms": 1_700_003_600_000,
                        "priority": 5,
                    }
                ],
            },
            now_ms=1_700_000_000_000,
        )
        app = BridgeApp(
            self.make_config("42"),
            telegram,
            worker,
            schedule_store=schedule_store,
        )

        app.handle_message(TelegramMessage(1, 42, 7, "/ask_day 我现在该做什么？"))

        self.assertIn("已携带日程状态加入队列", telegram.sent[-1][1])
        self.assertIn("写项目报告", worker.items[0].text)
        self.assertIn("我现在该做什么？", worker.items[0].text)

    def test_attachment_with_caption_is_saved_and_enqueued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "files"
            output = Path(tmp) / "out"
            telegram = FakeTelegram()
            telegram.downloads["photo-file"] = b"image"
            worker = FakeWorker()
            store = self.make_file_store(root, output)
            app = BridgeApp(
                self.make_config("42", file_root=root, output_root=output, files_enabled=True),
                telegram,
                worker,
                file_store=store,
            )

            app.handle_message(
                TelegramMessage(
                    1,
                    42,
                    7,
                    "请分析这张图",
                    TelegramAttachment("photo", "photo-file", "unique", "photo.jpg"),
                )
            )

            self.assertIn("文件已保存", telegram.sent[-1][1])
            self.assertEqual(len(worker.items), 1)
            self.assertIn("用户上传/引用了本机文件", worker.items[0].text)
            self.assertIn("请分析这张图", worker.items[0].text)
            self.assertIsNotNone(store.last_for_chat(42))

    def test_attachment_without_caption_only_updates_recent_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "files"
            output = Path(tmp) / "out"
            telegram = FakeTelegram()
            telegram.downloads["doc-file"] = b"text"
            worker = FakeWorker()
            store = self.make_file_store(root, output)
            app = BridgeApp(
                self.make_config("42", file_root=root, output_root=output, files_enabled=True),
                telegram,
                worker,
                file_store=store,
            )

            app.handle_message(
                TelegramMessage(
                    1,
                    42,
                    7,
                    "",
                    TelegramAttachment("document", "doc-file", "unique", "note.txt"),
                )
            )

            self.assertIn("已记录为最近上传文件", telegram.sent[-1][1])
            self.assertEqual(worker.items, [])
            self.assertIsNotNone(store.last_for_chat(42))

    def test_text_message_includes_recent_file_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "files"
            output = Path(tmp) / "out"
            telegram = FakeTelegram()
            worker = FakeWorker()
            store = self.make_file_store(root, output)
            store.save_upload(
                chat_id=42,
                original_name="note.txt",
                source="document",
                content=b"text",
            )
            app = BridgeApp(
                self.make_config("42", file_root=root, output_root=output, files_enabled=True),
                telegram,
                worker,
                file_store=store,
            )

            app.handle_message(TelegramMessage(1, 42, 7, "总结这个文件"))

            self.assertIn("用户上传/引用了本机文件", worker.items[0].text)
            self.assertIn("总结这个文件", worker.items[0].text)

    def test_file_commands_list_and_send_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "files"
            output = Path(tmp) / "out"
            telegram = FakeTelegram()
            worker = FakeWorker()
            store = self.make_file_store(root, output)
            stored = store.save_upload(
                chat_id=42,
                original_name="result.png",
                source="photo",
                content=b"image",
            )
            app = BridgeApp(
                self.make_config("42", file_root=root, output_root=output, files_enabled=True),
                telegram,
                worker,
                file_store=store,
            )

            app.handle_message(TelegramMessage(1, 42, 7, "/files"))
            app.handle_message(TelegramMessage(1, 42, 8, f"/photo {stored.id}"))
            app.handle_message(TelegramMessage(1, 42, 9, f"/file {stored.id}"))

            self.assertIn(stored.id, telegram.sent[0][1])
            self.assertEqual(telegram.sent_photos, [(42, stored.path)])
            self.assertEqual(telegram.sent_documents, [(42, stored.path)])

    def test_clear_file_context_removes_recent_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "files"
            output = Path(tmp) / "out"
            telegram = FakeTelegram()
            worker = FakeWorker()
            store = self.make_file_store(root, output)
            store.save_upload(
                chat_id=42,
                original_name="note.txt",
                source="document",
                content=b"text",
            )
            app = BridgeApp(
                self.make_config("42", file_root=root, output_root=output, files_enabled=True),
                telegram,
                worker,
                file_store=store,
            )

            app.handle_message(TelegramMessage(1, 42, 7, "/clear_files_context"))
            app.handle_message(TelegramMessage(1, 42, 8, "hello"))

            self.assertIn("已清除", telegram.sent[-2][1])
            self.assertEqual(worker.items[0].text, "hello")


if __name__ == "__main__":
    unittest.main()
