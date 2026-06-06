from __future__ import annotations

import http.client
import json
import time
import unittest

from spica_agent.phone import PhoneStateStore
from spica_agent.phone_http import PhoneHttpServer
from spica_agent.schedule import ScheduleReminder, ScheduleStateStore


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


def task_payload(task_id: str, title: str, now_ms: int, *, completed: bool = False) -> dict:
    return {
        "id": task_id,
        "title": title,
        "description": "important",
        "deadline_ms": now_ms + 60 * 60 * 1000,
        "is_completed": completed,
        "completed_at_ms": now_ms if completed else None,
        "created_at_ms": now_ms - 60 * 60 * 1000,
        "parent_id": None,
        "priority": 5,
    }


class PhoneHttpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.telegram = FakeTelegram()
        self.store = PhoneStateStore()
        self.schedule_store = ScheduleStateStore(
            non_work_packages=frozenset({"com.video"}),
            non_work_threshold_minutes=20,
        )
        self.schedule_callbacks: list[tuple[int, ScheduleReminder]] = []
        self.server = PhoneHttpServer(
            host="127.0.0.1",
            port=0,
            token="secret",
            store=self.store,
            telegram=self.telegram,
            notify_chat_ids=frozenset({42}),
            schedule_store=self.schedule_store,
            schedule_token="schedule-secret",
            schedule_reminder_callback=lambda chat_id, reminder: self.schedule_callbacks.append(
                (chat_id, reminder)
            ),
        )
        self.server.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.stop()

    def post(
        self,
        payload: dict,
        *,
        token: str = "secret",
        path: str = "/api/phone/events",
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload)
        connection.request(
            "POST",
            path,
            body=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()
        return response.status, json.loads(raw)

    def get(
        self,
        path: str,
        *,
        token: str = "schedule-secret",
    ) -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        connection.request(
            "GET",
            path,
            headers={"Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()
        return response.status, json.loads(raw)

    def test_accepts_events_and_sends_notifications(self) -> None:
        now_ms = int(time.time() * 1000)
        status, data = self.post(
            {
                "device_id": "device-1",
                "events": [
                    {
                        "event_id": "event-1",
                        "occurred_at_ms": now_ms,
                        "collected_at_ms": now_ms,
                        "type": "status",
                        "snapshot": {
                            "manufacturer": "Google",
                            "model": "Pixel",
                            "battery_percent": 90,
                            "is_charging": False,
                        },
                    }
                ],
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["accepted_event_ids"], ["event-1"])
        self.assertTrue(self.telegram.sent)

    def test_phone_events_update_schedule_store_and_invoke_reminder_callback(self) -> None:
        now_ms = int(time.time() * 1000)
        self.schedule_store.process_snapshot(
            {
                "device_id": "device-1",
                "today": "2026-06-04",
                "sent_at_ms": now_ms,
                "tasks": [task_payload("task-1", "写项目报告", now_ms)],
                "schedules": [],
            },
            now_ms=now_ms,
        )

        status, data = self.post(
            {
                "device_id": "device-1",
                "events": [
                    {
                        "event_id": "event-1",
                        "occurred_at_ms": now_ms + 60_000,
                        "collected_at_ms": now_ms + 60_000,
                        "type": "status",
                        "snapshot": {
                            "manufacturer": "Google",
                            "model": "Pixel",
                            "battery_percent": 90,
                            "is_charging": False,
                            "recent_apps": [
                                {
                                    "package_name": "com.video",
                                    "app_name": "Video",
                                    "total_time_ms": 25 * 60 * 1000,
                                }
                            ],
                        },
                    }
                ],
            }
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["schedule_reminder_count"], 1)
        self.assertEqual(self.schedule_callbacks[0][0], 42)
        self.assertIn("写项目报告", self.schedule_callbacks[0][1].text)
        self.assertIn("Video", self.schedule_callbacks[0][1].agent_prompt)

    def test_rejects_bad_token(self) -> None:
        status, data = self.post({"device_id": "device-1", "events": []}, token="bad")

        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_rejects_bad_payload(self) -> None:
        status, data = self.post({"device_id": "device-1"})

        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])

    def test_accepts_schedule_snapshot_and_invokes_reminder_callback(self) -> None:
        now_ms = int(time.time() * 1000)
        status, data = self.post(
            {
                "device_id": "device-1",
                "today": "2026-06-04",
                "tasks": [task_payload("task-1", "写项目报告", now_ms)],
                "schedules": [],
                "phone_status": {
                    "recent_apps": [
                        {
                            "package_name": "com.video",
                            "app_name": "Video",
                            "total_time_ms": 25 * 60 * 1000,
                        }
                    ]
                },
            },
            path="/api/schedule/snapshot",
            token="schedule-secret",
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["accepted_task_ids"], ["task-1"])
        self.assertEqual(data["accepted_schedule_ids"], [])
        self.assertEqual(data["reminder_count"], 1)
        self.assertEqual(self.schedule_callbacks[0][0], 42)
        self.assertIn("写项目报告", self.schedule_callbacks[0][1].agent_prompt)

    def test_schedule_uses_separate_token(self) -> None:
        status, data = self.post(
            {"tasks": [], "schedules": []},
            path="/api/schedule/snapshot",
            token="secret",
        )

        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_accepts_schedule_changes(self) -> None:
        status, data = self.post(
            {
                "changed_tasks": [task_payload("task-1", "写项目报告", int(time.time() * 1000), completed=True)],
                "changed_schedules": [],
            },
            path="/api/schedule/changes",
            token="schedule-secret",
        )

        self.assertEqual(status, 200)
        self.assertEqual(data["accepted_task_ids"], ["task-1"])
        self.assertEqual(data["accepted_schedule_ids"], [])

    def test_schedule_status_get_returns_private_current_state(self) -> None:
        now_ms = int(time.time() * 1000)
        self.schedule_store.process_snapshot(
            {
                "device_id": "device-1",
                "today": "2026-06-04",
                "sent_at_ms": now_ms,
                "tasks": [task_payload("task-1", "写项目报告", now_ms)],
                "schedules": [],
                "phone_status": {
                    "recent_apps": [
                        {
                            "package_name": "com.video",
                            "app_name": "Video",
                            "total_time_ms": 25 * 60 * 1000,
                        }
                    ]
                },
            },
            now_ms=now_ms,
        )

        status, data = self.get("/api/schedule/status")

        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["progress"]["total"], 1)
        self.assertEqual(
            data["data"]["phone_status"]["recent_apps"][0]["package_name"],
            "com.video",
        )

    def test_schedule_stateshare_get_returns_public_payload(self) -> None:
        now_ms = int(time.time() * 1000)
        self.schedule_store.process_snapshot(
            {
                "device_id": "device-1",
                "today": "2026-06-04",
                "tasks": [task_payload("task-1", "写项目报告", now_ms)],
                "schedules": [],
                "phone_status": {
                    "recent_apps": [
                        {
                            "package_name": "com.video",
                            "app_name": "Video",
                            "total_time_ms": 25 * 60 * 1000,
                        }
                    ]
                },
            },
            now_ms=now_ms,
        )

        status, data = self.get("/api/schedule/stateshare")

        self.assertEqual(status, 200)
        self.assertEqual(data["data"]["schedule"][0]["title"], "写项目报告")
        self.assertNotIn("com.video", json.dumps(data["data"], ensure_ascii=False))

    def test_schedule_get_requires_token(self) -> None:
        status, data = self.get("/api/schedule/status", token="bad")

        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
