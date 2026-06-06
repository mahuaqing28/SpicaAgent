from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spica_agent.config import AppConfig, ConfigError


class ConfigTests(unittest.TestCase):
    def make_base_env(self, path: Path) -> dict[str, str]:
        env_file = path / "deepseek.txt"
        env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
        return {
            "TELEGRAM_BOT_TOKEN": "token",
            "CLAUDE_WORKDIR": str(path),
            "CLAUDE_ENV_FILE": str(env_file),
        }

    def test_defaults_forward_proxy_env_names_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            config = AppConfig.from_env(self.make_base_env(path), cwd=path)

        self.assertIn("HTTPS_PROXY", config.claude_forward_env_vars)
        self.assertIn("http_proxy", config.claude_forward_env_vars)
        self.assertFalse(config.phone_bridge_enabled)
        self.assertEqual(config.phone_bridge_host, "0.0.0.0")
        self.assertEqual(config.phone_bridge_port, 8765)

    def test_rejects_invalid_forward_env_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            env = self.make_base_env(path)
            env["CLAUDE_FORWARD_ENV_VARS"] = "HTTPS_PROXY,bad-name"

            with self.assertRaises(ConfigError):
                AppConfig.from_env(env, cwd=path)

    def test_phone_bridge_requires_token_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            env = self.make_base_env(path)
            env["PHONE_BRIDGE_ENABLED"] = "true"

            with self.assertRaises(ConfigError):
                AppConfig.from_env(env, cwd=path)

    def test_phone_bridge_parses_notify_chat_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            env = self.make_base_env(path)
            env.update(
                {
                    "PHONE_BRIDGE_ENABLED": "true",
                    "PHONE_BRIDGE_TOKEN": "secret",
                    "PHONE_BRIDGE_PORT": "9001",
                    "PHONE_NOTIFY_CHAT_IDS": "42,43",
                }
            )
            config = AppConfig.from_env(env, cwd=path)

        self.assertTrue(config.phone_bridge_enabled)
        self.assertEqual(config.phone_bridge_token, "secret")
        self.assertEqual(config.phone_bridge_port, 9001)
        self.assertEqual(config.phone_notify_chat_ids, frozenset({42, 43}))

    def test_schedule_bridge_requires_token_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            env = self.make_base_env(path)
            env["SCHEDULE_BRIDGE_ENABLED"] = "true"

            with self.assertRaises(ConfigError):
                AppConfig.from_env(env, cwd=path)

    def test_schedule_bridge_parses_config_and_reuses_phone_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            state_file = path / "schedule.json"
            share_file = path / "status.json"
            env = self.make_base_env(path)
            env.update(
                {
                    "PHONE_BRIDGE_TOKEN": "shared-secret",
                    "SCHEDULE_BRIDGE_ENABLED": "true",
                    "SCHEDULE_STATE_FILE": str(state_file),
                    "SCHEDULE_STATESHARE_FILE": str(share_file),
                    "SCHEDULE_NON_WORK_PACKAGES": "com.video,com.social",
                    "SCHEDULE_NON_WORK_THRESHOLD_MINUTES": "15",
                    "SCHEDULE_REMINDER_COOLDOWN_MINUTES": "45",
                    "SCHEDULE_REMINDER_CHECK_INTERVAL_SECONDS": "30",
                }
            )
            config = AppConfig.from_env(env, cwd=path)

        self.assertTrue(config.schedule_bridge_enabled)
        self.assertEqual(config.schedule_bridge_token, "shared-secret")
        self.assertEqual(config.schedule_state_file, state_file.resolve())
        self.assertEqual(config.schedule_stateshare_file, share_file.resolve())
        self.assertEqual(
            config.schedule_non_work_packages,
            frozenset({"com.video", "com.social"}),
        )
        self.assertEqual(config.schedule_non_work_threshold_minutes, 15)
        self.assertEqual(config.schedule_reminder_cooldown_minutes, 45)
        self.assertEqual(config.schedule_reminder_check_interval_seconds, 30)


if __name__ == "__main__":
    unittest.main()
