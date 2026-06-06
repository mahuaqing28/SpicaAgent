from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    created_at_ms: int
    parent_id: str | None

    @property
    def status(self) -> str:
        if self.is_completed:
            return "done"
        return "todo"


@dataclass(frozen=True)
class ScheduleReminder:
    text: str
    agent_prompt: str
    use_agent: bool = True


@dataclass(frozen=True)
class ScheduleProcessResult:
    accepted_task_ids: list[str]
    accepted_schedule_ids: list[str]
    reminders: list[ScheduleReminder]


@dataclass(frozen=True)
class ScheduleEntry:
    id: str
    task_id: str
    title: str
    description: str
    date: str
    type: str
    start_time_ms: int | None
    end_time_ms: int | None
    deadline_ms: int | None
    is_completed: bool
    priority: int
    reminder_enabled: bool
    reminder_minutes_before: int

    @property
    def status(self) -> str:
        if self.is_completed:
            return "done"
        if self.start_time_ms is not None:
            return "doing"
        return "todo"

    @property
    def reminder_at_ms(self) -> int | None:
        if not self.reminder_enabled or self.is_completed:
            return None
        target_ms = self.deadline_ms or self.start_time_ms or self.end_time_ms
        if target_ms is None:
            return None
        return target_ms - max(self.reminder_minutes_before, 0) * 60 * 1000


class ScheduleStateStore:
    def __init__(
        self,
        *,
        state_file: Path | None = None,
        state_share_file: Path | None = None,
        non_work_packages: frozenset[str] = frozenset(),
        non_work_threshold_minutes: int = 20,
        reminder_cooldown_minutes: int = 120,
        agent_schedule_dir: Path | None = None,
        agent_history_days: int = 7,
    ) -> None:
        self._lock = Lock()
        self._state_file = state_file.expanduser().resolve() if state_file else None
        self._state_share_file = (
            state_share_file.expanduser().resolve() if state_share_file else None
        )
        self._agent_schedule_dir = (
            agent_schedule_dir.expanduser().resolve() if agent_schedule_dir else None
        )
        self._agent_history_days = max(agent_history_days, 1)
        self._non_work_packages = non_work_packages
        self._non_work_threshold_ms = non_work_threshold_minutes * 60 * 1000
        self._reminder_cooldown_ms = reminder_cooldown_minutes * 60 * 1000
        self._tasks: dict[str, ScheduleTask] = {}
        self._schedules: dict[str, ScheduleEntry] = {}
        self._phone_status: dict[str, Any] = {}
        self._device_id = ""
        self._timezone = "Asia/Shanghai"
        self._today = ""
        self._updated_at_ms = 0
        self._phone_updated_at_ms = 0
        self._reminder_at: dict[str, int] = {}
        self._load()

    def process_snapshot(
        self, payload: dict[str, Any], *, now_ms: int | None = None
    ) -> ScheduleProcessResult:
        now = _now_ms() if now_ms is None else now_ms
        tasks = [_parse_task(item) for item in _required_list(payload, "tasks")]
        schedules = _parse_schedules(_required_list(payload, "schedules"), tasks)
        phone_status = _optional_object(payload.get("phone_status"))
        device_id = str(payload.get("device_id") or self._device_id or "").strip()
        timezone_name = str(payload.get("timezone") or self._timezone or "Asia/Shanghai")
        today = str(payload.get("today") or _date_label(now))
        sent_at_ms = _optional_int(payload.get("sent_at_ms")) or now

        with self._lock:
            self._tasks = {task.id: task for task in tasks}
            self._schedules = {schedule.id: schedule for schedule in schedules}
            if phone_status is not None:
                self._phone_status = phone_status
                self._phone_updated_at_ms = sent_at_ms
            self._device_id = device_id
            self._timezone = timezone_name
            self._today = today
            self._updated_at_ms = sent_at_ms
            self._write_agent_files_locked(now)
            reminders = self._reminders_for_locked(now)
            self._save_locked()
            self._write_state_share_locked(now)
            self._write_agent_files_locked(now)

        return ScheduleProcessResult(
            accepted_task_ids=[task.id for task in tasks],
            accepted_schedule_ids=[schedule.id for schedule in schedules],
            reminders=reminders,
        )

    def process_changes(
        self, payload: dict[str, Any], *, now_ms: int | None = None
    ) -> ScheduleProcessResult:
        now = _now_ms() if now_ms is None else now_ms
        changed_tasks = [_parse_task(item) for item in _required_list(payload, "changed_tasks")]
        changed_schedules = _parse_schedules(
            _required_list(payload, "changed_schedules"),
            list(self._tasks.values()) + changed_tasks,
        )
        phone_status = _optional_object(payload.get("phone_status"))
        timezone_name = str(payload.get("timezone") or self._timezone or "Asia/Shanghai")
        today = str(payload.get("today") or self._today or _date_label(now))
        sent_at_ms = _optional_int(payload.get("sent_at_ms")) or now
        device_id = str(payload.get("device_id") or self._device_id or "").strip()

        with self._lock:
            changed_task_ids = {task.id for task in changed_tasks}
            for task in changed_tasks:
                self._tasks[task.id] = task
            for schedule in changed_schedules:
                self._schedules[schedule.id] = schedule
            changed_schedule_ids = {schedule.id for schedule in changed_schedules}
            for schedule_id, schedule in list(self._schedules.items()):
                if schedule_id in changed_schedule_ids or schedule.task_id not in changed_task_ids:
                    continue
                task = self._tasks.get(schedule.task_id)
                if task is not None:
                    self._schedules[schedule_id] = _schedule_with_task(schedule, task)
            if phone_status is not None:
                self._phone_status = phone_status
                self._phone_updated_at_ms = sent_at_ms
            self._device_id = device_id
            self._timezone = timezone_name
            self._today = today
            self._updated_at_ms = sent_at_ms
            self._write_agent_files_locked(now)
            reminders = self._reminders_for_locked(now)
            self._save_locked()
            self._write_state_share_locked(now)
            self._write_agent_files_locked(now)

        return ScheduleProcessResult(
            accepted_task_ids=[task.id for task in changed_tasks],
            accepted_schedule_ids=[schedule.id for schedule in changed_schedules],
            reminders=reminders,
        )

    def process_phone_status(
        self, phone_status: dict[str, Any], *, now_ms: int | None = None
    ) -> list[ScheduleReminder]:
        now = _now_ms() if now_ms is None else now_ms
        snapshot = _optional_object(phone_status)
        if snapshot is None:
            return []

        with self._lock:
            self._phone_status = snapshot
            self._phone_updated_at_ms = now
            if not self._today:
                self._today = _date_label(now)
            self._write_agent_files_locked(now)
            reminders = self._reminders_for_locked(now)
            self._save_locked()
            self._write_state_share_locked(now)
            self._write_agent_files_locked(now)
            return reminders

    def format_status(self) -> str:
        with self._lock:
            items = self._display_items_locked()
            updated_at_ms = self._updated_at_ms
            phone_status = dict(self._phone_status)

        if not items:
            return "尚未收到日程同步。"

        total = len(items)
        done = sum(1 for item in items if item.status == "done")
        lines = [
            "当前日程状态：",
            f"更新: {_format_time(updated_at_ms) if updated_at_ms else 'unknown'}",
            f"进度: {done}/{total}",
        ]
        focus = _foreground_label(phone_status)
        if focus:
            lines.append(f"最近应用: {focus}")
        lines.append("")
        for item in items[:12]:
            marker = "x" if item.status == "done" else ">"
            lines.append(f"- [{marker}] {_item_time_label(item)} {item.title} P{item.priority}")
        return "\n".join(lines)

    def status_payload(self) -> dict[str, Any]:
        with self._lock:
            tasks = self._sorted_tasks_locked()
            schedules = self._sorted_schedules_locked()
            phone_status = dict(self._phone_status)
            return {
                "device_id": self._device_id,
                "timezone": self._timezone,
                "today": self._today,
                "updated_at_ms": self._updated_at_ms,
                "phone_updated_at_ms": self._phone_updated_at_ms,
                "progress": _progress_payload(self._display_items_locked()),
                "tasks": [_task_to_json(task) for task in tasks],
                "schedules": [_schedule_to_json(schedule) for schedule in schedules],
                "phone_status": phone_status,
                "summary": self._format_status_locked(self._display_items_locked(), phone_status),
            }

    def state_share_payload(self, *, now_ms: int | None = None) -> dict[str, Any]:
        now = _now_ms() if now_ms is None else now_ms
        with self._lock:
            return self._state_share_payload_locked(now)

    def due_reminders(self, *, now_ms: int | None = None) -> list[ScheduleReminder]:
        now = _now_ms() if now_ms is None else now_ms
        with self._lock:
            reminders = self._due_schedule_reminders_locked(now)
            if reminders:
                self._save_locked()
                self._write_state_share_locked(now)
                self._write_agent_files_locked(now)
            return reminders

    def agent_context_message(self, question: str) -> str:
        with self._lock:
            self._write_agent_files_locked(_now_ms())
            if self._agent_schedule_dir is None:
                schedule_status = self._format_status_for_display_locked()
                return "\n".join(
                    [
                        "以下是 SpicaAgent bridge 当前记录的日程状态：",
                        schedule_status,
                        "",
                        "用户问题：",
                        question,
                    ]
                )
            hint_lines = self._agent_file_hint_lines_locked()
            summary = self._format_status_locked(
                self._display_items_locked(),
                self._phone_status,
            )
        return "\n".join(
            [
                "请根据 SpicaAgent 已同步到工作目录的日程文件回答用户问题。",
                "优先读取这些文件，而不是依赖本条消息中的摘要：",
                *hint_lines,
                "",
                f"当前简要摘要：{summary}",
                "",
                "用户问题：",
                question,
            ]
        )

    def _reminders_for_locked(self, now_ms: int) -> list[ScheduleReminder]:
        reminders = self._due_schedule_reminders_locked(now_ms)
        risky_tasks = _risky_tasks(list(self._tasks.values()), now_ms)
        distracting_apps = _distracting_apps(
            self._phone_status,
            self._non_work_packages,
            self._non_work_threshold_ms,
        )
        if not risky_tasks or not distracting_apps:
            return reminders

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
                    agent_prompt=_agent_prompt(
                        top_task,
                        app,
                        self._agent_file_hint_lines_locked(),
                        self._phone_status,
                    ),
                )
            )
        return reminders

    def _due_schedule_reminders_locked(self, now_ms: int) -> list[ScheduleReminder]:
        reminders: list[ScheduleReminder] = []
        for entry in self._sorted_schedules_locked():
            reminder_at = entry.reminder_at_ms
            if reminder_at is None or reminder_at > now_ms:
                continue
            key = f"deadline:{entry.id}:{reminder_at}"
            if key in self._reminder_at:
                continue
            self._reminder_at[key] = now_ms
            reminders.append(
                ScheduleReminder(
                    text=_deadline_reminder_text(entry, reminder_at),
                    agent_prompt="",
                    use_agent=False,
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
        schedules = data.get("schedules")
        if isinstance(schedules, list):
            parsed_schedules: dict[str, ScheduleEntry] = {}
            for item in schedules:
                if isinstance(item, dict):
                    try:
                        schedule = _parse_schedule(item, self._tasks)
                    except SchedulePayloadError:
                        continue
                    parsed_schedules[schedule.id] = schedule
            self._schedules = parsed_schedules
        self._phone_status = _optional_object(data.get("phone_status")) or {}
        self._device_id = str(data.get("device_id") or "")
        self._timezone = str(data.get("timezone") or "Asia/Shanghai")
        self._today = str(data.get("today") or "")
        self._updated_at_ms = _optional_int(data.get("updated_at_ms")) or 0
        self._phone_updated_at_ms = _optional_int(data.get("phone_updated_at_ms")) or 0
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
            "phone_updated_at_ms": self._phone_updated_at_ms,
            "phone_status": self._phone_status,
            "tasks": [_task_to_json(task) for task in self._tasks.values()],
            "schedules": [_schedule_to_json(schedule) for schedule in self._schedules.values()],
            "reminder_at": self._reminder_at,
        }
        self._state_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_agent_files_locked(self, now_ms: int) -> None:
        if self._agent_schedule_dir is None:
            return
        try:
            root = self._agent_schedule_dir
            daily_dir = root / "daily"
            root.mkdir(parents=True, exist_ok=True)
            daily_dir.mkdir(parents=True, exist_ok=True)

            _write_json_file(root / "current.json", self._agent_current_payload_locked(now_ms))
            _write_json_file(
                root / "tasks.json",
                {
                    "generated_at_ms": now_ms,
                    "generated_at": _format_time(now_ms),
                    "updated_at_ms": self._updated_at_ms,
                    "updated_at": (
                        _format_time(self._updated_at_ms)
                        if self._updated_at_ms
                        else "unknown"
                    ),
                    "tasks": [
                        _task_to_json(task) for task in self._sorted_tasks_locked()
                    ],
                },
            )
            for date, payload in self._daily_payloads_locked(now_ms).items():
                _write_json_file(daily_dir / f"{date}.json", payload)
            _write_text_file(root / "today.md", self._today_markdown_locked(now_ms))
            _prune_daily_files(daily_dir, self._agent_history_days, now_ms)
        except OSError:
            return

    def _agent_current_payload_locked(self, now_ms: int) -> dict[str, Any]:
        items = self._display_items_locked()
        return {
            "generated_at_ms": now_ms,
            "generated_at": _format_time(now_ms),
            "device_id": self._device_id,
            "timezone": self._timezone,
            "today": self._today or _date_label(now_ms),
            "updated_at_ms": self._updated_at_ms,
            "updated_at": _format_time(self._updated_at_ms) if self._updated_at_ms else "unknown",
            "phone_updated_at_ms": self._phone_updated_at_ms,
            "phone_updated_at": (
                _format_time(self._phone_updated_at_ms)
                if self._phone_updated_at_ms
                else "unknown"
            ),
            "progress": _progress_payload(items),
            "summary": self._format_status_locked(items, self._phone_status),
            "phone_status": self._phone_status,
            "tasks": [_task_to_json(task) for task in self._sorted_tasks_locked()],
            "schedules": [
                _schedule_to_json(schedule) for schedule in self._sorted_schedules_locked()
            ],
            "display_items": [_item_to_agent_json(item) for item in items],
        }

    def _daily_payloads_locked(self, now_ms: int) -> dict[str, dict[str, Any]]:
        today = self._today or _date_label(now_ms)
        dates = {today}
        for schedule in self._schedules.values():
            if schedule.date:
                dates.add(schedule.date)

        payloads: dict[str, dict[str, Any]] = {}
        for date in sorted(dates):
            if date == today:
                items = self._display_items_locked()
            else:
                items = [
                    schedule
                    for schedule in self._sorted_schedules_locked()
                    if schedule.date == date
                ]
            payloads[date] = {
                "date": date,
                "generated_at_ms": now_ms,
                "generated_at": _format_time(now_ms),
                "updated_at_ms": self._updated_at_ms,
                "progress": _progress_payload(items),
                "summary": self._format_status_locked(items, self._phone_status),
                "items": [_item_to_agent_json(item) for item in items],
                "schedules": [
                    _schedule_to_json(schedule)
                    for schedule in self._sorted_schedules_locked()
                    if schedule.date == date
                ],
            }
        return payloads

    def _today_markdown_locked(self, now_ms: int) -> str:
        items = self._display_items_locked()
        progress = _progress_payload(items)
        lines = [
            "# Spica Schedule",
            "",
            f"- Generated: {_format_time(now_ms)}",
            f"- Today: {self._today or _date_label(now_ms)}",
            f"- Updated: {_format_time(self._updated_at_ms) if self._updated_at_ms else 'unknown'}",
            f"- Phone updated: {_format_time(self._phone_updated_at_ms) if self._phone_updated_at_ms else 'unknown'}",
            f"- Progress: {progress['done']}/{progress['total']}",
        ]
        focus = _foreground_label(self._phone_status)
        if focus:
            lines.append(f"- Recent app: {focus}")
        lines.extend(["", "## Current Items"])
        if not items:
            lines.append("- No schedule data received yet.")
        for item in items[:24]:
            marker = "x" if item.status == "done" else " "
            lines.append(
                f"- [{marker}] {_item_time_label(item)} P{item.priority} {item.title}"
            )
        lines.extend(
            [
                "",
                "## Files",
                f"- current: {self._agent_schedule_dir / 'current.json' if self._agent_schedule_dir else 'current.json'}",
                f"- tasks: {self._agent_schedule_dir / 'tasks.json' if self._agent_schedule_dir else 'tasks.json'}",
                f"- daily: {self._agent_schedule_dir / 'daily' if self._agent_schedule_dir else 'daily'}",
            ]
        )
        return "\n".join(lines) + "\n"

    def _agent_file_hint_lines_locked(self) -> list[str]:
        if self._agent_schedule_dir is None:
            return ["- 日程文件未配置，请使用本条消息中的摘要。"]
        return [
            f"- {self._agent_schedule_dir / 'today.md'}",
            f"- {self._agent_schedule_dir / 'current.json'}",
            f"- {self._agent_schedule_dir / 'tasks.json'}",
            f"- {self._agent_schedule_dir / 'daily'}",
        ]

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
        self, items: list[ScheduleTask | ScheduleEntry], phone_status: dict[str, Any]
    ) -> str:
        if not items:
            return "尚未收到日程同步。"
        total = len(items)
        done = sum(1 for item in items if item.status == "done")
        focus = _foreground_label(phone_status)
        parts = [f"进度 {done}/{total}"]
        if focus:
            parts.append(f"最近应用 {focus}")
        active = next((item for item in items if item.status != "done"), None)
        if active is not None:
            parts.append(f"下一项 {active.title}")
        return "，".join(parts)

    def _format_status_for_display_locked(self) -> str:
        items = self._display_items_locked()
        if not items:
            return "尚未收到日程同步。"

        total = len(items)
        done = sum(1 for item in items if item.status == "done")
        lines = [
            "当前日程状态：",
            f"更新: {_format_time(self._updated_at_ms) if self._updated_at_ms else 'unknown'}",
            f"进度: {done}/{total}",
        ]
        focus = _foreground_label(self._phone_status)
        if focus:
            lines.append(f"最近应用: {focus}")
        lines.append("")
        for item in items[:12]:
            marker = "x" if item.status == "done" else ">"
            lines.append(f"- [{marker}] {_item_time_label(item)} {item.title} P{item.priority}")
        return "\n".join(lines)

    def _state_share_payload_locked(self, now_ms: int) -> dict[str, Any]:
        items = self._display_items_locked()
        total = len(items)
        done = sum(1 for item in items if item.status == "done")
        progress = int(done * 100 / total) if total else 0
        schedule = [
            {
                "time": _item_time_label(item),
                "title": item.title,
                "note": item.description,
                "status": item.status,
            }
            for item in items
            if (
                item.deadline_ms is not None
                or (isinstance(item, ScheduleEntry) and item.start_time_ms is not None)
                or item.status != "done"
            )
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

    def _sorted_tasks_locked(self) -> list[ScheduleTask]:
        return sorted(
            self._tasks.values(),
            key=lambda task: (
                task.is_completed,
                task.deadline_ms is None,
                task.deadline_ms or 0,
                -task.priority,
            ),
        )

    def _sorted_schedules_locked(self) -> list[ScheduleEntry]:
        return sorted(
            self._schedules.values(),
            key=lambda schedule: (
                schedule.is_completed,
                schedule.start_time_ms is None and schedule.deadline_ms is None,
                schedule.start_time_ms or schedule.deadline_ms or 0,
                -schedule.priority,
            ),
        )

    def _display_items_locked(self) -> list[ScheduleTask | ScheduleEntry]:
        schedules = self._sorted_schedules_locked()
        scheduled_task_ids = {schedule.task_id for schedule in schedules}
        unscheduled_tasks = [
            task for task in self._sorted_tasks_locked() if task.id not in scheduled_task_ids
        ]
        return [*schedules, *unscheduled_tasks]


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
    is_completed = _required_bool(raw, "is_completed", "task")
    created_at_ms = _required_int(raw, "created_at_ms", "task")
    return ScheduleTask(
        id=task_id,
        title=title,
        description=str(raw.get("description") or "").strip(),
        deadline_ms=_optional_int(raw.get("deadline_ms")),
        is_completed=is_completed,
        completed_at_ms=_optional_int(raw.get("completed_at_ms")),
        priority=_coerce_priority(raw.get("priority")),
        created_at_ms=created_at_ms,
        parent_id=_optional_str(raw.get("parent_id")),
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
        "created_at_ms": task.created_at_ms,
        "parent_id": task.parent_id,
    }


def _parse_schedules(
    raw_schedules: list[Any],
    tasks: list[ScheduleTask],
) -> list[ScheduleEntry]:
    task_map = {task.id: task for task in tasks}
    return [_parse_schedule(item, task_map) for item in raw_schedules]


def _parse_schedule(
    raw: Any,
    tasks: dict[str, ScheduleTask],
) -> ScheduleEntry:
    if not isinstance(raw, dict):
        raise SchedulePayloadError("schedule must be an object")
    raw_id = raw.get("id")
    if raw_id is None:
        raise SchedulePayloadError("schedule.id is required")
    schedule_id = str(raw_id).strip()
    if not schedule_id:
        raise SchedulePayloadError("schedule.id must not be empty")

    raw_task_id = raw.get("task_id")
    if raw_task_id is None:
        raise SchedulePayloadError("schedule.task_id is required")
    task_id = str(raw_task_id).strip()
    if not task_id:
        raise SchedulePayloadError("schedule.task_id must not be empty")

    task = tasks.get(task_id)
    if task is None:
        raise SchedulePayloadError(f"schedule.task_id references unknown task: {task_id}")

    date = str(raw.get("date") or "").strip()
    if not date:
        raise SchedulePayloadError("schedule.date is required")
    schedule_type = str(raw.get("type") or "").strip()
    if not schedule_type:
        raise SchedulePayloadError("schedule.type is required")
    start_time_ms = _optional_int(raw.get("start_time_ms"))
    end_time_ms = _optional_int(raw.get("end_time_ms"))
    reminder_enabled = _required_bool(raw, "reminder_enabled", "schedule")
    reminder_minutes_before = _coerce_non_negative_int(
        raw.get("reminder_minutes_before"),
        default=10,
    )
    return ScheduleEntry(
        id=schedule_id,
        task_id=task_id,
        title=task.title,
        description=task.description,
        date=date,
        type=schedule_type,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
        deadline_ms=task.deadline_ms,
        is_completed=task.is_completed,
        priority=task.priority,
        reminder_enabled=reminder_enabled,
        reminder_minutes_before=reminder_minutes_before,
    )


def _schedule_to_json(schedule: ScheduleEntry) -> dict[str, Any]:
    return {
        "id": schedule.id,
        "task_id": schedule.task_id,
        "title": schedule.title,
        "description": schedule.description,
        "date": schedule.date,
        "type": schedule.type,
        "start_time_ms": schedule.start_time_ms,
        "end_time_ms": schedule.end_time_ms,
        "deadline_ms": schedule.deadline_ms,
        "is_completed": schedule.is_completed,
        "priority": schedule.priority,
        "reminder_enabled": schedule.reminder_enabled,
        "reminder_minutes_before": schedule.reminder_minutes_before,
    }


def _item_to_agent_json(item: ScheduleTask | ScheduleEntry) -> dict[str, Any]:
    if isinstance(item, ScheduleEntry):
        payload = _schedule_to_json(item)
        payload["kind"] = "schedule"
        payload["time_label"] = _item_time_label(item)
        payload["status"] = item.status
        return payload
    payload = _task_to_json(item)
    payload["kind"] = "task"
    payload["time_label"] = _item_time_label(item)
    payload["status"] = item.status
    return payload


def _schedule_with_task(schedule: ScheduleEntry, task: ScheduleTask) -> ScheduleEntry:
    return ScheduleEntry(
        id=schedule.id,
        task_id=schedule.task_id,
        title=task.title,
        description=task.description,
        date=schedule.date,
        type=schedule.type,
        start_time_ms=schedule.start_time_ms,
        end_time_ms=schedule.end_time_ms,
        deadline_ms=task.deadline_ms,
        is_completed=task.is_completed,
        priority=task.priority,
        reminder_enabled=schedule.reminder_enabled,
        reminder_minutes_before=schedule.reminder_minutes_before,
    )


def _deadline_reminder_text(entry: ScheduleEntry, reminder_at_ms: int) -> str:
    target_ms = entry.deadline_ms or entry.start_time_ms or entry.end_time_ms
    deadline = _format_time(target_ms) if target_ms else "未设置具体时间"
    reminder_at = _format_time(reminder_at_ms)
    return "\n".join(
        [
            "日程提醒：",
            f"任务：{entry.title}",
            f"时间：{deadline}",
            f"提醒触发：{reminder_at}",
        ]
    )


def _progress_payload(items: list[ScheduleTask | ScheduleEntry]) -> dict[str, int]:
    total = len(items)
    done = sum(1 for item in items if item.status == "done")
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
    if not non_work_packages:
        return []
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
        if package not in non_work_packages:
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
    file_hint_lines: list[str],
    phone_status: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "你是 Spica 的日程监督 agent。请先读取工作目录中的日程文件，再判断是否需要给用户发一个简短提醒，并直接给出提醒文案。",
            "",
            "触发原因：",
            f"- 关键任务：{task.title}",
            f"- 优先级：P{task.priority}",
            f"- deadline：{_time_label(task.deadline_ms)}",
            f"- 非工作应用：{app['app_name']} ({app['package_name']})",
            f"- 最近使用：{int(app['total_time_ms'] // 60000)} 分钟",
            "",
            "请读取这些文件获取完整任务库、今日日程和手机状态：",
            *file_hint_lines,
            "",
            "提醒策略：",
            "- 如果确实需要介入，只输出一条自然、短促、可执行的提醒。",
            "- 如果不需要介入，输出一句“不提醒”并说明极短理由。",
            "",
            "手机状态摘要：",
            _phone_summary(phone_status),
        ]
    )


def _phone_summary(phone_status: dict[str, Any]) -> str:
    battery = phone_status.get("battery_percent", "unknown")
    foreground = _foreground_label(phone_status) or "unknown"
    return f"battery={battery}, foreground={foreground}"


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _prune_daily_files(daily_dir: Path, history_days: int, now_ms: int) -> None:
    today = _date_label(now_ms)
    dated_files: list[tuple[str, Path]] = []
    for path in daily_dir.glob("*.json"):
        date = path.stem
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            continue
        dated_files.append((date, path))

    dated_files.sort()
    keep_dates = {date for date, _ in dated_files[-history_days:]}
    cutoff = _date_before(today, history_days - 1)
    for date, path in dated_files:
        if date in keep_dates and date >= cutoff:
            continue
        try:
            path.unlink()
        except OSError:
            continue


def _date_before(date: str, days: int) -> str:
    try:
        parsed = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return date
    return (parsed - timedelta(days=days)).strftime("%Y-%m-%d")


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


def _required_bool(payload: dict[str, Any], name: str, label: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise SchedulePayloadError(f"{label}.{name} must be a boolean")
    return value


def _required_int(payload: dict[str, Any], name: str, label: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchedulePayloadError(f"{label}.{name} must be an integer")
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


def _coerce_non_negative_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return max(value, 0)
    return default


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


def _item_time_label(item: ScheduleTask | ScheduleEntry) -> str:
    if isinstance(item, ScheduleEntry) and item.start_time_ms is not None:
        start = _time_label(item.start_time_ms)
        if item.end_time_ms is not None:
            return f"{start}-{_time_label(item.end_time_ms)}"
        return start
    return _time_label(item.deadline_ms)


def _date_label(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")


def _format_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def _now_ms() -> int:
    return int(time.time() * 1000)
