from __future__ import annotations

import http.client
import json
import time
import unittest

from spica_agent.phone import PhoneStateStore
from spica_agent.phone_http import PhoneHttpServer


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.sent.append((chat_id, text))


class PhoneHttpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.telegram = FakeTelegram()
        self.store = PhoneStateStore()
        self.server = PhoneHttpServer(
            host="127.0.0.1",
            port=0,
            token="secret",
            store=self.store,
            telegram=self.telegram,
            notify_chat_ids=frozenset({42}),
        )
        self.server.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.stop()

    def post(self, payload: dict, *, token: str = "secret") -> tuple[int, dict]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=5)
        body = json.dumps(payload)
        connection.request(
            "POST",
            "/api/phone/events",
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

    def test_rejects_bad_token(self) -> None:
        status, data = self.post({"device_id": "device-1", "events": []}, token="bad")

        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_rejects_bad_payload(self) -> None:
        status, data = self.post({"device_id": "device-1"})

        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])


if __name__ == "__main__":
    unittest.main()
