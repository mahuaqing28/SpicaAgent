from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from spica_agent.schedule import SchedulePayloadError, ScheduleStateStore


NOW = 1_700_000_000_000


def task(
    task_id: str,
    title: str,
    *,
    deadline_ms: int | None = NOW + 60 * 60 * 1000,
    is_completed: bool = False,
    priority: int = 5,
) -> dict:
    return {
        "id": task_id,
        "title": title,
        "description": "important",
        "deadline_ms": deadline_ms,
        "is_completed": is_completed,
        "completed_at_ms": NOW + 1000 if is_completed else None,
        "created_at_ms": NOW - 60 * 60 * 1000,
        "parent_id": None,
        "priority": priority,
    }


def schedule(
    schedule_id: str,
    task_id: str,
    *,
    date: str = "2023-11-14",
    schedule_type: str = "FLOATING",
    start_time_ms: int | None = None,
    end_time_ms: int | None = None,
    reminder_enabled: bool = False,
    reminder_minutes_before: int = 10,
) -> dict:
    return {
        "id": schedule_id,
        "task_id": task_id,
        "date": date,
        "type": schedule_type,
        "start_time_ms": start_time_ms,
        "end_time_ms": end_time_ms,
        "reminder_enabled": reminder_enabled,
        "reminder_minutes_before": reminder_minutes_before,
    }


def phone_status(*, package_name: str = "com.video", minutes: int = 25) -> dict:
    return {
        "battery_percent": 88,
        "foreground_app": {"package_name": package_name, "app_name": "Video"},
        "recent_apps": [
            {
                "package_name": package_name,
                "app_name": "Video",
                "total_time_ms": minutes * 60 * 1000,
            }
        ],
    }


def init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "spica@example.test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Spica Test"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def git_output(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


class ScheduleStateStoreTests(unittest.TestCase):
    def test_snapshot_triggers_reminder_for_risky_task_and_non_work_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "schedule"
            store = ScheduleStateStore(
                non_work_packages=frozenset({"com.video"}),
                non_work_threshold_minutes=20,
                reminder_cooldown_minutes=120,
                agent_schedule_dir=agent_dir,
            )

            result = store.process_snapshot(
                {
                    "device_id": "phone-1",
                    "today": "2023-11-14",
                    "tasks": [task("1", "写项目报告")],
                    "schedules": [],
                    "phone_status": phone_status(),
                },
                now_ms=NOW,
            )

            self.assertEqual(result.accepted_task_ids, ["1"])
            self.assertEqual(len(result.reminders), 1)
            self.assertIn("写项目报告", result.reminders[0].text)
            self.assertIn("Video", result.reminders[0].agent_prompt)
            self.assertIn(str(agent_dir / "current.json"), result.reminders[0].agent_prompt)

    def test_reminder_has_cooldown(self) -> None:
        store = ScheduleStateStore(
            non_work_packages=frozenset({"com.video"}),
            non_work_threshold_minutes=20,
            reminder_cooldown_minutes=120,
        )
        payload = {
            "device_id": "phone-1",
            "today": "2023-11-14",
            "tasks": [task("1", "写项目报告")],
            "schedules": [],
            "phone_status": phone_status(),
        }

        first = store.process_snapshot(payload, now_ms=NOW)
        second = store.process_snapshot(payload, now_ms=NOW + 60_000)
        third = store.process_snapshot(payload, now_ms=NOW + 3 * 60 * 60 * 1000)

        self.assertEqual(len(first.reminders), 1)
        self.assertEqual(second.reminders, [])
        self.assertEqual(len(third.reminders), 1)

    def test_ignores_non_matching_or_short_app_usage(self) -> None:
        store = ScheduleStateStore(
            non_work_packages=frozenset({"com.video"}),
            non_work_threshold_minutes=20,
        )

        result = store.process_snapshot(
            {
                "tasks": [task("1", "写项目报告")],
                "schedules": [],
                "phone_status": phone_status(package_name="com.editor", minutes=40),
            },
            now_ms=NOW,
        )
        short = store.process_snapshot(
            {
                "tasks": [task("1", "写项目报告")],
                "schedules": [],
                "phone_status": phone_status(minutes=5),
            },
            now_ms=NOW + 3 * 60 * 60 * 1000,
        )

        self.assertEqual(result.reminders, [])
        self.assertEqual(short.reminders, [])

    def test_requires_configured_non_work_packages_for_agent_reminder(self) -> None:
        store = ScheduleStateStore(non_work_threshold_minutes=20)

        result = store.process_snapshot(
            {
                "tasks": [task("1", "写项目报告")],
                "schedules": [],
                "phone_status": phone_status(package_name="com.video", minutes=40),
            },
            now_ms=NOW,
        )

        self.assertEqual(result.reminders, [])

    def test_phone_status_update_can_trigger_risky_task_reminder(self) -> None:
        store = ScheduleStateStore(
            non_work_packages=frozenset({"com.video"}),
            non_work_threshold_minutes=20,
        )
        store.process_snapshot(
            {
                "device_id": "phone-1",
                "today": "2023-11-14",
                "tasks": [task("1", "写项目报告")],
                "schedules": [],
            },
            now_ms=NOW,
        )

        reminders = store.process_phone_status(phone_status(), now_ms=NOW + 60_000)

        self.assertEqual(len(reminders), 1)
        self.assertIn("写项目报告", reminders[0].text)
        self.assertIn("Video", reminders[0].agent_prompt)

    def test_writes_agent_schedule_files_and_prunes_old_daily_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            agent_dir = Path(tmp) / "schedule"
            daily_dir = agent_dir / "daily"
            daily_dir.mkdir(parents=True)
            old_file = daily_dir / "2023-11-01.json"
            old_file.write_text("{}", encoding="utf-8")
            keep_file = daily_dir / "2023-11-13.json"
            keep_file.write_text("{}", encoding="utf-8")
            store = ScheduleStateStore(agent_schedule_dir=agent_dir, agent_history_days=7)

            store.process_snapshot(
                {
                    "device_id": "phone-1",
                    "today": "2023-11-14",
                    "sent_at_ms": NOW,
                    "tasks": [task("1", "写项目报告")],
                    "schedules": [schedule("s1", "1", date="2023-11-14")],
                    "phone_status": phone_status(package_name="com.video", minutes=25),
                },
                now_ms=NOW,
            )

            self.assertTrue((agent_dir / "current.json").is_file())
            self.assertTrue((agent_dir / "tasks.json").is_file())
            self.assertTrue((agent_dir / "today.md").is_file())
            self.assertTrue((daily_dir / "2023-11-14.json").is_file())
            self.assertTrue(keep_file.is_file())
            self.assertFalse(old_file.exists())

            current = json.loads((agent_dir / "current.json").read_text(encoding="utf-8"))
            tasks = json.loads((agent_dir / "tasks.json").read_text(encoding="utf-8"))
            self.assertEqual(current["progress"]["total"], 1)
            self.assertEqual(tasks["tasks"][0]["title"], "写项目报告")
            self.assertIn("写项目报告", (agent_dir / "today.md").read_text(encoding="utf-8"))

    def test_persists_state_and_writes_public_stateshare_without_app_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "schedule-state.json"
            share_file = Path(tmp) / "status.json"
            store = ScheduleStateStore(
                state_file=state_file,
                state_share_file=share_file,
                state_share_owner="2049",
                non_work_packages=frozenset({"com.video"}),
            )

            store.process_snapshot(
                {
                    "device_id": "phone-1",
                    "today": "2023-11-14",
                    "sent_at_ms": NOW,
                    "tasks": [
                        task("1", "写项目报告", is_completed=True),
                        task("2", "跑步", priority=2),
                    ],
                    "schedules": [
                        schedule(
                            "s2",
                            "2",
                            schedule_type="TIME_BLOCK",
                            start_time_ms=NOW,
                            end_time_ms=NOW + 30 * 60 * 1000,
                        )
                    ],
                    "state_share": {
                        "tagline": "今日主线在线",
                        "funny_status": "状态稳定",
                        "today_bgm": "Night Drive",
                    },
                    "phone_status": phone_status(),
                },
                now_ms=NOW,
            )

            self.assertTrue(state_file.is_file())
            share = json.loads(share_file.read_text(encoding="utf-8"))
            expected_start = datetime.fromtimestamp(NOW / 1000).strftime("%H:%M")
            expected_end = datetime.fromtimestamp((NOW + 30 * 60 * 1000) / 1000).strftime(
                "%H:%M"
            )
            self.assertEqual(share["owner"], "2049")
            self.assertEqual(share["tagline"], "今日主线在线")
            self.assertEqual(share["funnyStatus"], "状态稳定")
            self.assertEqual(share["today_bgm"], "Night Drive")
            self.assertEqual(
                share["schedule"],
                [
                    {
                        "date": "2023-11-14",
                        "title": "跑步",
                        "type": "TIME_BLOCK",
                        "start_time": expected_start,
                        "end_time": expected_end,
                    }
                ],
            )
            self.assertNotIn("progress", share)
            self.assertNotIn("energy", share)
            self.assertNotIn("focus", share)
            self.assertNotIn("status", json.dumps(share, ensure_ascii=False))
            self.assertNotIn("note", json.dumps(share, ensure_ascii=False))
            self.assertNotIn("com.video", json.dumps(share, ensure_ascii=False))

            loaded = ScheduleStateStore(state_file=state_file)
            self.assertIn("写项目报告", loaded.format_status())
            self.assertEqual(loaded.state_share_payload(now_ms=NOW)["today_bgm"], "Night Drive")

    def test_stateshare_auto_commit_commits_file_and_skips_no_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "stateShare"
            repo.mkdir()
            init_git_repo(repo)
            share_file = repo / "data" / "status.json"
            store = ScheduleStateStore(
                state_share_file=share_file,
                state_share_auto_commit=True,
                state_share_repo=repo,
                state_share_push=False,
            )
            payload = {
                "today": "2023-11-14",
                "sent_at_ms": NOW,
                "tasks": [task("1", "写项目报告")],
                "schedules": [schedule("s1", "1")],
            }

            store.process_snapshot(payload, now_ms=NOW)

            self.assertTrue(share_file.is_file())
            self.assertEqual(git_output(repo, "rev-list", "--count", "HEAD"), "1")
            self.assertEqual(git_output(repo, "status", "--short"), "")
            self.assertIn("Update status", git_output(repo, "log", "-1", "--pretty=%s"))

            store.process_snapshot(payload, now_ms=NOW)

            self.assertEqual(git_output(repo, "rev-list", "--count", "HEAD"), "1")

    def test_stateshare_auto_commit_failure_does_not_fail_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "not-a-git-repo"
            share_file = repo / "data" / "status.json"
            store = ScheduleStateStore(
                state_share_file=share_file,
                state_share_auto_commit=True,
                state_share_repo=repo,
                state_share_push=False,
            )

            with self.assertLogs("spica_agent.schedule", level="WARNING") as logs:
                result = store.process_snapshot(
                    {
                        "today": "2023-11-14",
                        "tasks": [task("1", "写项目报告")],
                        "schedules": [schedule("s1", "1")],
                    },
                    now_ms=NOW,
                )

            self.assertEqual(result.accepted_task_ids, ["1"])
            self.assertTrue(share_file.is_file())
            self.assertIn("stateShare git status failed", "\n".join(logs.output))

    def test_status_payload_contains_private_state_and_public_payload_hides_packages(self) -> None:
        store = ScheduleStateStore(non_work_packages=frozenset({"com.video"}))
        store.process_snapshot(
            {
                "device_id": "phone-1",
                "today": "2023-11-14",
                "sent_at_ms": NOW,
                "tasks": [
                    task("1", "写项目报告", is_completed=True),
                    task("2", "跑步", priority=2),
                ],
                "schedules": [schedule("s2", "2")],
                "phone_status": phone_status(),
            },
            now_ms=NOW,
        )

        private = store.status_payload()
        public = store.state_share_payload(now_ms=NOW)

        self.assertEqual(private["progress"]["percent"], 50)
        self.assertEqual(private["phone_status"]["recent_apps"][0]["package_name"], "com.video")
        self.assertIn("写项目报告", [item["title"] for item in private["tasks"]])
        self.assertIn("跑步", private["summary"])
        self.assertEqual(public["owner"], "2049")
        self.assertEqual(public["schedule"][0]["title"], "跑步")
        self.assertEqual(public["schedule"][0]["type"], "FLOATING")
        self.assertNotIn("progress", public)
        self.assertNotIn("com.video", json.dumps(public, ensure_ascii=False))

    def test_changes_update_existing_task(self) -> None:
        store = ScheduleStateStore()
        store.process_snapshot(
            {"tasks": [task("1", "写项目报告", is_completed=False)], "schedules": []},
            now_ms=NOW,
        )

        result = store.process_changes(
            {
                "changed_tasks": [task("1", "写项目报告", is_completed=True)],
                "changed_schedules": [],
            },
            now_ms=NOW + 1000,
        )

        self.assertEqual(result.accepted_task_ids, ["1"])
        self.assertIn("进度: 1/1", store.format_status())

    def test_accepts_split_task_and_schedule_payload(self) -> None:
        store = ScheduleStateStore()

        result = store.process_snapshot(
            {
                "tasks": [
                    task("12", "写项目报告", deadline_ms=NOW + 60 * 60 * 1000)
                ],
                "schedules": [
                    schedule(
                        "99",
                        "12",
                        schedule_type="TIME_BLOCK",
                        start_time_ms=NOW,
                        end_time_ms=NOW + 30 * 60 * 1000,
                        reminder_enabled=True,
                    )
                ],
            },
            now_ms=NOW,
        )

        self.assertEqual(result.accepted_task_ids, ["12"])
        self.assertEqual(result.accepted_schedule_ids, ["99"])
        status = store.format_status()
        self.assertIn("写项目报告", status)
        self.assertIn("P5", status)

    def test_due_schedule_reminder_uses_schedule_settings_without_agent(self) -> None:
        store = ScheduleStateStore()

        result = store.process_snapshot(
            {
                "tasks": [task("1", "写项目报告", deadline_ms=NOW + 10 * 60 * 1000)],
                "schedules": [
                    schedule(
                        "1",
                        "1",
                        reminder_enabled=True,
                        reminder_minutes_before=10,
                    )
                ],
            },
            now_ms=NOW,
        )

        self.assertEqual(len(result.reminders), 1)
        self.assertFalse(result.reminders[0].use_agent)
        self.assertIn("写项目报告", result.reminders[0].text)

    def test_task_change_refreshes_existing_schedule_projection(self) -> None:
        store = ScheduleStateStore()
        store.process_snapshot(
            {
                "tasks": [task("1", "写项目报告", is_completed=False)],
                "schedules": [schedule("1", "1")],
            },
            now_ms=NOW,
        )

        result = store.process_changes(
            {
                "changed_tasks": [task("1", "写项目报告", is_completed=True)],
                "changed_schedules": [],
            },
            now_ms=NOW + 1000,
        )

        self.assertEqual(result.accepted_schedule_ids, [])
        self.assertIn("进度: 1/1", store.format_status())

    def test_changes_can_update_and_preserve_state_share_metadata(self) -> None:
        store = ScheduleStateStore()
        store.process_snapshot(
            {
                "today": "2023-11-14",
                "tasks": [task("1", "写项目报告", is_completed=False)],
                "schedules": [schedule("1", "1")],
                "state_share": {
                    "tagline": "旧主线",
                    "funny_status": "旧状态",
                    "today_bgm": "旧 BGM",
                },
            },
            now_ms=NOW,
        )

        store.process_changes(
            {
                "changed_tasks": [task("1", "写项目报告", is_completed=True)],
                "changed_schedules": [],
                "state_share": {"tagline": "新主线", "today_bgm": "新 BGM"},
            },
            now_ms=NOW + 1000,
        )
        updated = store.state_share_payload(now_ms=NOW + 1000)
        self.assertEqual(updated["tagline"], "新主线")
        self.assertEqual(updated["funnyStatus"], "")
        self.assertEqual(updated["today_bgm"], "新 BGM")

        store.process_changes(
            {
                "changed_tasks": [task("1", "写项目报告", is_completed=False)],
                "changed_schedules": [],
            },
            now_ms=NOW + 2000,
        )
        preserved = store.state_share_payload(now_ms=NOW + 2000)
        self.assertEqual(preserved["tagline"], "新主线")
        self.assertEqual(preserved["today_bgm"], "新 BGM")

    def test_rejects_bad_payload(self) -> None:
        store = ScheduleStateStore()

        with self.assertRaises(SchedulePayloadError):
            store.process_snapshot({"tasks": [{"id": "1"}], "schedules": []}, now_ms=NOW)

        with self.assertRaises(SchedulePayloadError):
            store.process_snapshot(
                {
                    "tasks": [task("1", "写项目报告")],
                    "schedules": [],
                    "state_share": "bad",
                },
                now_ms=NOW,
            )


if __name__ == "__main__":
    unittest.main()
