from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from spica_agent.config import AppConfig
from spica_agent.tmux_bridge import TmuxBridge


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class TmuxBridgeTests(unittest.TestCase):
    def make_config(self, workdir: Path, env_file: Path) -> AppConfig:
        return AppConfig.from_env(
            {
                "TELEGRAM_BOT_TOKEN": "token",
                "CLAUDE_WORKDIR": str(workdir),
                "CLAUDE_ENV_FILE": str(env_file),
                "CLAUDE_COMMAND": "claude --model test",
                "CLAUDE_FORWARD_ENV_VARS": "",
            },
            cwd=workdir,
        )

    def test_ensure_session_creates_tmux_with_env_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env_file = workdir / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            config = self.make_config(workdir, env_file)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(args)
                if args[:2] == ["tmux", "has-session"]:
                    return completed(returncode=1)
                return completed()

            bridge = TmuxBridge(
                config,
                {"API_KEY": "secret"},
                run=runner,
                which=lambda command: f"/usr/bin/{command}",
            )

            bridge.ensure_session()

        new_session = next(call for call in calls if call[:2] == ["tmux", "new-session"])
        self.assertIn("-e", new_session)
        self.assertIn("API_KEY=secret", new_session)
        self.assertEqual(new_session[-1], "claude --model test")

    def test_restart_session_kills_and_recreates_existing_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env_file = workdir / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            config = self.make_config(workdir, env_file)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(args)
                if args[:2] == ["tmux", "has-session"]:
                    return completed(returncode=0)
                return completed()

            bridge = TmuxBridge(
                config,
                {"API_KEY": "secret"},
                run=runner,
                which=lambda command: f"/usr/bin/{command}",
            )

            bridge.restart_session()

        self.assertIn(["tmux", "kill-session", "-t", "claude_bg"], calls)
        self.assertTrue(any(call[:2] == ["tmux", "new-session"] for call in calls))

    def test_send_text_uses_tmux_buffer_not_shell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env_file = workdir / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            config = self.make_config(workdir, env_file)
            calls: list[tuple[list[str], str | None]] = []

            def runner(args, **kwargs):
                calls.append((args, kwargs.get("input")))
                return completed()

            bridge = TmuxBridge(
                config,
                {},
                run=runner,
                which=lambda command: f"/usr/bin/{command}",
            )

            bridge.send_text('hello "world"\nsecond line')

        self.assertEqual(
            calls[0][0],
            [
                "tmux",
                "send-keys",
                "-t",
                "claude_bg",
                "-l",
                "--",
                'hello "world"\nsecond line',
            ],
        )
        self.assertIsNone(calls[0][1])
        self.assertEqual(calls[1][0], ["tmux", "send-keys", "-t", "claude_bg", "C-m"])

    def test_capture_screen_limits_history_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env_file = workdir / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            config = AppConfig.from_env(
                {
                    "TELEGRAM_BOT_TOKEN": "token",
                    "CLAUDE_WORKDIR": str(workdir),
                    "CLAUDE_ENV_FILE": str(env_file),
                    "CLAUDE_CAPTURE_LINES": "500",
                    "CLAUDE_FORWARD_ENV_VARS": "",
                },
                cwd=workdir,
            )
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(args)
                return completed(stdout="hello")

            bridge = TmuxBridge(
                config,
                {},
                run=runner,
                which=lambda command: f"/usr/bin/{command}",
            )

            self.assertEqual(bridge.capture_screen(), "hello")

        self.assertEqual(calls[0][calls[0].index("-S") + 1], "-500")

    def test_clear_input_sends_ctrl_u(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env_file = workdir / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            config = self.make_config(workdir, env_file)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(args)
                return completed()

            bridge = TmuxBridge(
                config,
                {},
                run=runner,
                which=lambda command: f"/usr/bin/{command}",
            )

            bridge.clear_input()

        self.assertEqual(calls[0], ["tmux", "send-keys", "-t", "claude_bg", "C-u"])

    def test_send_keys_sends_terminal_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            env_file = workdir / "deepseek.txt"
            env_file.write_text("export API_KEY=secret\n", encoding="utf-8")
            config = self.make_config(workdir, env_file)
            calls: list[list[str]] = []

            def runner(args, **kwargs):
                calls.append(args)
                return completed()

            bridge = TmuxBridge(
                config,
                {},
                run=runner,
                which=lambda command: f"/usr/bin/{command}",
            )

            bridge.send_keys("Down", "Enter")

        self.assertEqual(calls[0], ["tmux", "send-keys", "-t", "claude_bg", "Down"])
        self.assertEqual(calls[1], ["tmux", "send-keys", "-t", "claude_bg", "Enter"])


if __name__ == "__main__":
    unittest.main()
