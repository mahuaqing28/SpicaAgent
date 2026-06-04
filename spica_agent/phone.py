from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any


DAY_MS = 24 * 60 * 60 * 1000
OFFLINE_DELAY_MS = 2 * 60 * 1000
LOW_BATTERY_PERCENT = 20
LOW_BATTERY_RESET_PERCENT = 25
LONG_APP_USE_MS = 30 * 60 * 1000
LONG_APP_COOLDOWN_MS = 2 * 60 * 60 * 1000


class PhonePayloadError(ValueError):
    """Raised when an Android phone status payload is malformed."""


@dataclass(frozen=True)
class PhoneEvent:
    device_id: str
    event_id: str
    occurred_at_ms: int
    collected_at_ms: int
    type: str
    snapshot: dict[str, Any]


@dataclass(frozen=True)
class PhoneProcessResult:
    accepted_event_ids: list[str]
    notifications: list[str]


class PhoneStateStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._latest_by_device: dict[str, PhoneEvent] = {}
        self._seen_event_ids: set[str] = set()
        self._connected_devices: set[str] = set()
        self._low_battery_notified: set[str] = set()
        self._app_notify_at: dict[tuple[str, str], int] = {}

    def process_payload(
        self, payload: dict[str, Any], *, now_ms: int | None = None
    ) -> PhoneProcessResult:
        events = _parse_payload(payload)
        now = _now_ms() if now_ms is None else now_ms
        accepted: list[str] = []
        notifications: list[str] = []

        with self._lock:
            for event in events:
                accepted.append(event.event_id)
                if event.event_id in self._seen_event_ids:
                    continue
                self._seen_event_ids.add(event.event_id)
                self._latest_by_device[event.device_id] = event

                if now - event.occurred_at_ms > DAY_MS:
                    continue
                notifications.extend(self._notifications_for(event, now))

        return PhoneProcessResult(accepted_event_ids=accepted, notifications=notifications)

    def format_latest_status(self) -> str:
        with self._lock:
            events = sorted(
                self._latest_by_device.values(),
                key=lambda event: event.occurred_at_ms,
                reverse=True,
            )

        if not events:
            return "尚未收到手机状态。"

        lines = ["最近手机状态："]
        for event in events:
            lines.extend(_format_event_status(event))
            lines.append("")
        lines.append("注：手机端离线待补发队列只在手机本地可见。")
        return "\n".join(lines).rstrip()

    def _notifications_for(self, event: PhoneEvent, now_ms: int) -> list[str]:
        messages: list[str] = []
        prefix = _notification_prefix(event, now_ms)
        device = _device_label(event)

        if event.device_id not in self._connected_devices:
            self._connected_devices.add(event.device_id)
            messages.append(f"{prefix}手机已连接：{device}。")

        battery_percent = _number(event.snapshot.get("battery_percent"))
        is_charging = bool(event.snapshot.get("is_charging"))
        if battery_percent is not None:
            if battery_percent < LOW_BATTERY_PERCENT and not is_charging:
                if event.device_id not in self._low_battery_notified:
                    self._low_battery_notified.add(event.device_id)
                    messages.append(f"{prefix}{device} 电量较低：{battery_percent:.0f}%，且未充电。")
            elif is_charging or battery_percent >= LOW_BATTERY_RESET_PERCENT:
                self._low_battery_notified.discard(event.device_id)

        for app in _usage_apps(event.snapshot):
            package_name = str(app.get("package_name") or app.get("package") or "").strip()
            if not package_name:
                continue
            total_time_ms = _number(app.get("total_time_ms")) or 0
            if total_time_ms < LONG_APP_USE_MS:
                continue
            key = (event.device_id, package_name)
            last_notified = self._app_notify_at.get(key)
            if last_notified is not None and event.occurred_at_ms - last_notified < LONG_APP_COOLDOWN_MS:
                continue
            self._app_notify_at[key] = event.occurred_at_ms
            app_name = str(app.get("app_name") or package_name)
            minutes = int(total_time_ms // 60000)
            messages.append(f"{prefix}{device} 最近 30 分钟使用 {app_name} 约 {minutes} 分钟。")

        return messages


def _parse_payload(payload: dict[str, Any]) -> list[PhoneEvent]:
    device_id = str(payload.get("device_id", "")).strip()
    raw_events = payload.get("events")
    if not device_id:
        raise PhonePayloadError("device_id is required")
    if not isinstance(raw_events, list):
        raise PhonePayloadError("events must be a list")

    events: list[PhoneEvent] = []
    for raw in raw_events:
        if not isinstance(raw, dict):
            raise PhonePayloadError("events must contain objects")
        event_id = str(raw.get("event_id", "")).strip()
        event_type = str(raw.get("type", "")).strip() or "status"
        snapshot = raw.get("snapshot")
        if not event_id:
            raise PhonePayloadError("event_id is required")
        if not isinstance(snapshot, dict):
            raise PhonePayloadError("snapshot must be an object")
        occurred_at_ms = _required_int(raw, "occurred_at_ms")
        collected_at_ms = _required_int(raw, "collected_at_ms")
        events.append(
            PhoneEvent(
                device_id=device_id,
                event_id=event_id,
                occurred_at_ms=occurred_at_ms,
                collected_at_ms=collected_at_ms,
                type=event_type,
                snapshot=dict(snapshot),
            )
        )
    return events


def _required_int(raw: dict[str, Any], name: str) -> int:
    value = raw.get(name)
    if not isinstance(value, int):
        raise PhonePayloadError(f"{name} must be an integer")
    return value


def _format_event_status(event: PhoneEvent) -> list[str]:
    snapshot = event.snapshot
    battery = _number(snapshot.get("battery_percent"))
    charging = "充电中" if snapshot.get("is_charging") else "未充电"
    network = snapshot.get("network_type") or "unknown"
    usage_access = "已授权" if snapshot.get("usage_access_granted") else "未授权"
    foreground = _foreground_label(snapshot)

    lines = [
        f"- 设备: {_device_label(event)}",
        f"  上报时间: {_format_time(event.occurred_at_ms)}",
        f"  电量: {_format_percent(battery)} ({charging})",
        f"  网络: {network}",
        f"  Usage Access: {usage_access}",
        f"  最近应用: {foreground}",
    ]
    summary = _usage_summary(snapshot)
    if summary:
        lines.append(f"  近 30 分钟: {summary}")
    return lines


def _usage_apps(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    value = snapshot.get("recent_apps")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _usage_summary(snapshot: dict[str, Any]) -> str:
    parts: list[str] = []
    for app in _usage_apps(snapshot)[:5]:
        name = str(app.get("app_name") or app.get("package_name") or "unknown")
        total_time_ms = _number(app.get("total_time_ms")) or 0
        minutes = int(total_time_ms // 60000)
        if minutes > 0:
            parts.append(f"{name} {minutes}m")
    return ", ".join(parts)


def _foreground_label(snapshot: dict[str, Any]) -> str:
    foreground = snapshot.get("foreground_app")
    if isinstance(foreground, dict):
        return str(foreground.get("app_name") or foreground.get("package_name") or "unknown")
    if isinstance(foreground, str) and foreground:
        return foreground
    apps = _usage_apps(snapshot)
    if apps:
        return str(apps[0].get("app_name") or apps[0].get("package_name") or "unknown")
    return "unknown"


def _device_label(event: PhoneEvent) -> str:
    snapshot = event.snapshot
    manufacturer = str(snapshot.get("manufacturer") or "").strip()
    model = str(snapshot.get("model") or event.device_id).strip()
    android = str(snapshot.get("android_release") or "").strip()
    label = " ".join(part for part in [manufacturer, model] if part)
    if android:
        label += f" (Android {android})"
    return label or event.device_id


def _notification_prefix(event: PhoneEvent, now_ms: int) -> str:
    if now_ms - event.occurred_at_ms >= OFFLINE_DELAY_MS:
        return f"离线期间（{_format_time(event.occurred_at_ms)}）："
    return ""


def _format_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _format_percent(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value:.0f}%"


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _now_ms() -> int:
    return int(time.time() * 1000)
