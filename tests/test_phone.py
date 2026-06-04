from __future__ import annotations

import unittest

from spica_agent.phone import DAY_MS, PhonePayloadError, PhoneStateStore


NOW = 1_700_000_000_000


def payload(
    event_id: str,
    *,
    occurred_at_ms: int = NOW,
    battery_percent: int = 90,
    is_charging: bool = False,
    recent_apps: list[dict] | None = None,
) -> dict:
    return {
        "device_id": "device-1",
        "events": [
            {
                "event_id": event_id,
                "occurred_at_ms": occurred_at_ms,
                "collected_at_ms": occurred_at_ms,
                "type": "status",
                "snapshot": {
                    "manufacturer": "Google",
                    "model": "Pixel",
                    "android_release": "14",
                    "battery_percent": battery_percent,
                    "is_charging": is_charging,
                    "network_type": "wifi",
                    "usage_access_granted": True,
                    "recent_apps": recent_apps or [],
                },
            }
        ],
    }


class PhoneStateStoreTests(unittest.TestCase):
    def test_accepts_event_and_sends_first_connection_notification(self) -> None:
        store = PhoneStateStore()

        result = store.process_payload(payload("event-1"), now_ms=NOW)

        self.assertEqual(result.accepted_event_ids, ["event-1"])
        self.assertEqual(len(result.notifications), 1)
        self.assertIn("手机已连接", result.notifications[0])

    def test_duplicate_event_is_accepted_but_not_reprocessed(self) -> None:
        store = PhoneStateStore()
        store.process_payload(payload("event-1"), now_ms=NOW)

        result = store.process_payload(payload("event-1"), now_ms=NOW)

        self.assertEqual(result.accepted_event_ids, ["event-1"])
        self.assertEqual(result.notifications, [])

    def test_low_battery_notification_is_deduplicated_until_reset(self) -> None:
        store = PhoneStateStore()
        first = store.process_payload(payload("event-1", battery_percent=10), now_ms=NOW)
        second = store.process_payload(payload("event-2", battery_percent=9), now_ms=NOW)
        store.process_payload(
            payload("event-3", battery_percent=40, is_charging=True), now_ms=NOW
        )
        third = store.process_payload(payload("event-4", battery_percent=8), now_ms=NOW)

        self.assertTrue(any("电量较低" in item for item in first.notifications))
        self.assertFalse(any("电量较低" in item for item in second.notifications))
        self.assertTrue(any("电量较低" in item for item in third.notifications))

    def test_long_app_usage_has_cooldown(self) -> None:
        store = PhoneStateStore()
        apps = [
            {
                "package_name": "com.example.video",
                "app_name": "Video",
                "total_time_ms": 31 * 60 * 1000,
            }
        ]

        first = store.process_payload(
            payload("event-1", recent_apps=apps), now_ms=NOW
        )
        second = store.process_payload(
            payload("event-2", occurred_at_ms=NOW + 60_000, recent_apps=apps),
            now_ms=NOW + 60_000,
        )
        third = store.process_payload(
            payload(
                "event-3",
                occurred_at_ms=NOW + 3 * 60 * 60 * 1000,
                recent_apps=apps,
            ),
            now_ms=NOW + 3 * 60 * 60 * 1000,
        )

        self.assertTrue(any("Video" in item for item in first.notifications))
        self.assertFalse(any("Video" in item for item in second.notifications))
        self.assertTrue(any("Video" in item for item in third.notifications))

    def test_offline_notification_is_prefixed(self) -> None:
        store = PhoneStateStore()

        result = store.process_payload(
            payload("event-1", occurred_at_ms=NOW - 10 * 60 * 1000),
            now_ms=NOW,
        )

        self.assertTrue(result.notifications[0].startswith("离线期间"))

    def test_events_older_than_day_update_status_but_skip_rules(self) -> None:
        store = PhoneStateStore()

        result = store.process_payload(
            payload("event-1", occurred_at_ms=NOW - DAY_MS - 1),
            now_ms=NOW,
        )

        self.assertEqual(result.notifications, [])
        self.assertIn("Google Pixel", store.format_latest_status())

    def test_rejects_malformed_payload(self) -> None:
        store = PhoneStateStore()

        with self.assertRaises(PhonePayloadError):
            store.process_payload({"device_id": "device-1", "events": [{}]})


if __name__ == "__main__":
    unittest.main()
