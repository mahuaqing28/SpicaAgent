from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


DAY_MS = 24 * 60 * 60 * 1000


class SchedulePayloadError(ValueError):
    """Raised when a schedule bridge payload is malformed."""


@dataclass(frozen=True)
class ScheduleTask:
    id: str
    title: str
    description: str
    deadline_ms: int | None
    is_completed: bool
    completed_at_ms: int | None
    priority: int
    started_at_ms: int | None
    parent_id: str | None
    protocol: str
    updated_at_ms: int | None

    @property
    def status(self) -> str:
        if self.is_completed:
            return "done"
        if self.started_at_ms:
            return "doing"
        return "todo"


@dataclass(frozen=True)
class ScheduleReminder:
    text: str
    agent_prompt: str


@dataclass(frozen=True)
class ScheduleProcessResult:
    accepted_task_ids: list[str]
    reminders: list[ScheduleReminder]


class ScheduleStateStore:
    def __init__(
        self,
        *,
        state_file: Path | None = None,
        state_share_file: Path | None = None,
        non_work_packages: frozenset[str] = frozenset(),
        non_work_threshold_minutes: int = 20,
        reminder_cooldown_minutes: int = 120,
    ) -> None:
        self._lock = Lock()
        self._state_file = state_file.expanduser().resolve() if state_file else None
        self._state_share_file = (
            state_share_file.expanduser().resolve() if state_share_file else None
        )
        self._non_work_packages = non_work_packages
        self._non_work_threshold_ms = non_work_threshold_minutes * 60 * 1000
        self._reminder_cooldown_ms = reminder_cooldown_minutes * 60 * 1000
        self._tasks: dict[str, ScheduleTask] = {}
        self._phone_status: dict[str, Any] = {}
        self._device_id = ""
        self._timezone = "Asia/Shanghai"
        self._today = ""
        self._updated_at_ms = 0
        self._reminder_at: dict[str, int] = {}
        self._load()

    def process_snapshot(
        self, payload: dict[str, Any], *, now_ms: int | None = None
    ) -> ScheduleProcessResult:
        now = _now_ms() if now_ms is None else now_ms
        tasks = [_parse_task(item) for item in _required_list(payload, "tasks")]
        phone_status = _optional_object(payload.get("phone_status"))
        device_id = str(payload.get("device_id") or self._device_id or "").strip()
        timezone_name = str(payload.get("timezone") or self._timezone or "Asia/Shanghai")
        today = str(payload.get("today") or _date_label(now))
        sent_at_ms = _optional_int(payload.get("sent_at_ms")) or now

        with self._lock:
            self._tasks = {task.id: task for task in tasks}
            if phone_status is not None:
                self._phone_status = phone_status
            self._device_id = device_id
            self._timezone = timezone_name
            self._today = today
            self._updated_at_ms = sent_at_ms
            reminders = self._reminders_for_locked(now)
            self._save_locked()
            self._write_state_share_locked(now)

        return ScheduleProcessResult(
            accepted_task_ids=[task.id for task in tasks],
            reminders=reminders,
        )

    def process_changes(
        self, payload: dict[str, Any], *, now_ms: int | None = None
    ) -> ScheduleProcessResult:
        now = _now_ms() if now_ms is None else now_ms
        changed_tasks = [_parse_task(item) for item in _required_list(payload, "changed_tasks")]
        phone_status = _optional_object(payload.get("phone_status"))
        timezone_name = str(payload.get("timezone") or self._timezone or "Asia/Shanghai")
        today = str(payload.get("today") or self._today or _date_label(now))
        sent_at_ms = _optional_int(payload.get("sent_at_ms")) or now
        device_id = str(payload.get("device_id") or self._device_id or "").strip()

        with self._lock:
            for task in changed_tasks:
                self._tasks[task.id] = task
            if phone_status is not None:
                self._phone_status = phone_status
            self._device_id = device_id
            self._timezone = timezone_name
            self._today = today
            self._updated_at_ms = sent_at_ms
            reminders = self._reminders_for_locked(now)
            self._save_locked()
            self._write_state_share_locked(now)

        return ScheduleProcessResult(
            accepted_task_ids=[task.id for task in changed_tasks],
            reminders=reminders,
        )

    def format_status(self) -> str:
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda task: (
                    task.is_completed,
                    task.deadline_ms is None,
                    task.deadline_ms or 0,
                    -task.priority,
                ),
            )
            updated_at_ms = self._updated_at_ms
            phone_status = dict(self._phone_status)

        if not tasks:
            return "尚未收到日程同步。"

        total = len(tasks)
        done = sum(1 for task in tasks if task.is_completed)
        lines = [
            "当前日程状态：",
            f"更新: {_format_time(updated_at_ms) if updated_at_ms else 'unknown'}",
            f"进度: {done}/{total}",
        ]
        focus = _foreground_label(phone_status)
        if focus:
            lines.append(f"最近应用: {focus}")
        lines.append("")
        for task in tasks[:12]:
            marker = "x" if task.is_completed else ">"
            deadline = _time_label(task.deadline_ms)
            lines.append(f"- [{marker}] {deadline} {task.title} P{task.priority}")
        return "\n".join(lines)

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            tasks = sorted(
                self._tasks.values(),
                key=lambda task: (
                    task.is_completed,
                    task.deadline_ms is None,
                    task.deadline_ms or 0,
                    -task.priority,
                ),
            )
            phone_status = dict(self._phone_status)
            return {
                "device_id": self._device_id,
                "timezone": self._timezone,
                "today": self._today,
                "updated_at_ms": self._updated_at_ms,
                "progress": _progress_payload(tasks),
                "tasks": [_task_to_json(task) for task in tasks],
                "phone_status": phone_status,
                "summary": self._format_status_locked(tasks, phone_status),
            }

    def state_share_payload(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = _now_ms() if now_ms is None else now_ms
        with self._lock:
            return self._state_share_payload_locked(now)

    def _reminders_for_locked(self, now_ms: int) -> list[ScheduleReminder]:
        risky_tasks = _risky_tasks(list(self._tasks.values()), now_ms)
        distracting_apps = _distracting_apps(
            self._phone_status,
            self._non_work_packages,
            self._non_work_threshold_ms,
        )
        if not risky_tasks or not distracting_apps:
            return []

        reminders: list[ScheduleReminder] = []
        top_task = risky_tasks[0]
        for app in distracting_apps:
            package = app["package_name"]
            key = f"{self._today}:{package}:{top_task.id}"
            last_at = self._reminder_at.get(key)
            if last_at is not None and now_ms - last_at < self._reminder_cooldown_ms:
                continue
            self._reminder_at[key] = now_ms
            minutes = int(app["total_time_ms"] // 60000)
            text = (
                f"日程提醒候选：当前还有「{top_task.title}」未完成，"
                f"但最近非工作应用 {app['app_name']} 使用约 {minutes} 分钟。"
            )
            reminders.append(
                ScheduleReminder(
                    text=text,
                    agent_prompt=_agent_prompt(top_task, app, list(self._tasks.values()), self._phone_status),
                )
            )
        return reminders

    def _load(self) -> None:
        if self._state_file is None or not self._state_file.is_file():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        tasks = data.get("tasks")
        if isinstance(tasks, list):
            parsed: dict[str, ScheduleTask] = {}
            for item in tasks:
                if isinstance(item, dict):
                    try:
                        task = _parse_task(item)
                    except SchedulePayloadError:
                        continue
                    parsed[task.id] = task
            self._tasks = parsed
        self._phone_status = _optional_object(data.get("phone_status")) or {}
        self._device_id = str(data.get("device_id") or "")
        self._timezone = str(data.get("timezone") or "Asia/Shanghai")
        self._today = str(data.get("today") or "")
        self._updated_at_ms = _optional_int(data.get("updated_at_ms")) or 0
        reminder_at = data.get("reminder_at")
        if isinstance(reminder_at, dict):
            self._reminder_at = {
                str(key): value
                for key, value in reminder_at.items()
                if isinstance(value, int)
            }

    def _save_locked(self) -> None:
        if self._state_file is None:
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "device_id": self._device_id,
            "timezone": self._timezone,
            "today": self._today,
            "updated_at_ms": self._updated_at_ms,
            "phone_status": self._phone_status,
            "tasks": [_task_to_json(task) for task in self._tasks.values()],
            "reminder_at": self._reminder_at,
        }
        self._state_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_state_share_locked(self, now_ms: int) -> None:
        if self._state_share_file is None:
            return
        self._state_share_file.parent.mkdir(parents=True, exist_ok=True)
        payload = self._state_share_payload_locked(now_ms)
        self._state_share_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _format_status_locked(
        self, tasks: list[ScheduleTask], phone_status: dict[str, Any]
    ) -> str:
        if not tasks:
            return "尚未收到日程同步。"
        total = len(tasks)
        done = sum(1 for task in tasks if task.is_completed)
        focus = _foreground_label(phone_status)
        parts = [f"进度 {done}/{total}"]
        if focus:
            parts.append(f"最近应用 {focus}")
        active = next((task for task in tasks if not task.is_completed), None)
        if active is not None:
            parts.append(f"下一项 {active.title}")
        return "，".join(parts)

    def _state_share_payload_locked(self, now_ms: int) -> dict[str, Any]:
        tasks = sorted(
            self._tasks.values(),
            key=lambda task: (task.deadline_ms is None, task.deadline_ms or 0, -task.priority),
        )
        total = len(tasks)
        done = sum(1 for task in tasks if task.is_completed)
        progress = int(done * 100 / total) if total else 0
        schedule = [
            {
                "time": _time_label(task.deadline_ms),
                "title": task.title,
                "note": task.description,
                "status": task.status,
            }
            for task in tasks
            if task.deadline_ms is not None or not task.is_completed
        ][:12]
        payload = {
            "owner": "mahuaqing",
            "today": self._today or _date_label(now_ms),
            "tagline": "当前状态由 Spica 云端 bridge 自动同步。",
            "updatedAt": _format_time(self._updated_at_ms or now_ms),
            "energy": _energy_percent(self._phone_status),
            "focus": _focus_percent(self._phone_status, self._non_work_packages),
            "progress": progress,
            "funnyStatus": _public_funny_status(done, total),
            "progressNote": f"今日已完成 {done}/{total} 项。",
            "schedule": schedule,
        }
        return payload


def _parse_task(raw: Any) -> ScheduleTask:
    if not isinstance(raw, dict):
        raise SchedulePayloadError("task must be an object")
    raw_id = raw.get("id")
    if raw_id is None:
        raise SchedulePayloadError("task.id is required")
    task_id = str(raw_id).strip()
    title = str(raw.get("title") or "").strip()
    if not task_id:
        raise SchedulePayloadError("task.id must not be empty")
    if not title:
        raise SchedulePayloadError("task.title is required")
    return ScheduleTask(
        id=task_id,
        title=title,
        description=str(raw.get("description") or raw.get("note") or "").strip(),
        deadline_ms=_optional_int(raw.get("deadline_ms", raw.get("deadline"))),
        is_completed=bool(raw.get("is_completed", raw.get("isCompleted", False))),
        completed_at_ms=_optional_int(raw.get("completed_at_ms", raw.get("completedAt"))),
        priority=_coerce_priority(raw.get("priority")),
        started_at_ms=_optional_int(raw.get("started_at_ms", raw.get("startedAt"))),
        parent_id=_optional_str(raw.get("parent_id", raw.get("parentId"))),
        protocol=str(raw.get("protocol") or "DUTY"),
        updated_at_ms=_optional_int(raw.get("updated_at_ms", raw.get("updatedAt"))),
    )


def _task_to_json(task: ScheduleTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "deadline_ms": task.deadline_ms,
        "is_completed": task.is_completed,
        "completed_at_ms": task.completed_at_ms,
        "priority": task.priority,
        "started_at_ms": task.started_at_ms,
        "parent_id": task.parent_id,
        "protocol": task.protocol,
        "updated_at_ms": task.updated_at_ms,
    }


def _progress_payload(tasks: list[ScheduleTask]) -> dict[str, int]:
    total = len(tasks)
    done = sum(1 for task in tasks if task.is_completed)
    percent = int(done * 100 / total) if total else 0
    return {
        "total": total,
        "done": done,
        "active": total - done,
        "percent": percent,
    }


def _risky_tasks(tasks: list[ScheduleTask], now_ms: int) -> list[ScheduleTask]:
    candidates = [
        task
        for task in tasks
        if not task.is_completed
        and (
            task.priority >= 4
            or (
                task.deadline_ms is not None
                and task.deadline_ms <= now_ms + 2 * 60 * 60 * 1000
            )
        )
    ]
    return sorted(
        candidates,
        key=lambda task: (
            task.deadline_ms is None,
            task.deadline_ms or now_ms + DAY_MS,
            -task.priority,
        ),
    )


def _distracting_apps(
    phone_status: dict[str, Any],
    non_work_packages: frozenset[str],
    threshold_ms: int,
) -> list[dict[str, Any]]:
    apps = phone_status.get("recent_apps")
    if not isinstance(apps, list):
        return []
    result: list[dict[str, Any]] = []
    for item in apps:
        if not isinstance(item, dict):
            continue
        package = str(item.get("package_name") or item.get("package") or "").strip()
        if not package:
            continue
        if non_work_packages and package not in non_work_packages:
            continue
        total_time_ms = _optional_int(item.get("total_time_ms")) or 0
        if total_time_ms < threshold_ms:
            continue
        result.append(
            {
                "package_name": package,
                "app_name": str(item.get("app_name") or package),
                "total_time_ms": total_time_ms,
            }
        )
    return sorted(result, key=lambda app: app["total_time_ms"], reverse=True)


def _agent_prompt(
    task: ScheduleTask,
    app: dict[str, Any],
    tasks: list[ScheduleTask],
    phone_status: dict[str, Any],
) -> str:
    unfinished = [item for item in tasks if not item.is_completed]
    task_lines = [
        f"- {item.title} priority={item.priority} deadline={_time_label(item.deadline_ms)}"
        for item in sorted(unfinished, key=lambda item: (-item.priority, item.deadline_ms or 0))[:8]
    ]
    return "\n".join(
        [
            "你是 Spica 的日程监督 agent。请判断是否需要给用户发一个简短提醒，并直接给出提醒文案。",
            "",
            "当前风险：",
            f"- 关键任务：{task.title}",
            f"- 非工作应用：{app['app_name']} ({app['package_name']})",
            f"- 最近使用：{int(app['total_time_ms'] // 60000)} 分钟",
            "",
            "未完成任务：",
            *task_lines,
            "",
            "手机状态摘要：",
            _phone_summary(phone_status),
        ]
    )


def _phone_summary(phone_status: dict[str, Any]) -> str:
    battery = phone_status.get("battery_percent", "unknown")
    foreground = _foreground_label(phone_status) or "unknown"
    return f"battery={battery}, foreground={foreground}"


def _foreground_label(phone_status: dict[str, Any]) -> str:
    foreground = phone_status.get("foreground_app")
    if isinstance(foreground, dict):
        return str(foreground.get("app_name") or foreground.get("package_name") or "")
    if isinstance(foreground, str):
        return foreground
    apps = phone_status.get("recent_apps")
    if isinstance(apps, list) and apps:
        first = apps[0]
        if isinstance(first, dict):
            return str(first.get("app_name") or first.get("package_name") or "")
    return ""


def _required_list(payload: dict[str, Any], name: str) -> list[Any]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise SchedulePayloadError(f"{name} must be a list")
    return value


def _optional_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SchedulePayloadError("phone_status must be an object")
    return dict(value)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_priority(value: Any) -> int:
    if isinstance(value, bool):
        return 3
    if isinstance(value, int):
        return min(max(value, 1), 5)
    return 3


def _energy_percent(phone_status: dict[str, Any]) -> int:
    battery = _optional_int(phone_status.get("battery_percent"))
    if battery is None:
        return 70
    return min(max(battery, 0), 100)


def _focus_percent(phone_status: dict[str, Any], non_work_packages: frozenset[str]) -> int:
    apps = _distracting_apps(phone_status, non_work_packages, threshold_ms=1)
    minutes = sum(app["total_time_ms"] for app in apps) // 60000
    return int(min(max(100 - minutes * 2, 0), 100))


def _public_funny_status(done: int, total: int) -> str:
    if total == 0:
        return "今日状态正在同步中。"
    if done >= total:
        return "今日主线已清空，状态稳定。"
    return "正在推进今日主线，欢迎监督但不要投喂干扰。"


def _time_label(ms: int | None) -> str:
    if ms is None:
        return "--:--"
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def _date_label(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _format_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
