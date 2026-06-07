from __future__ import annotations

import unittest

from spica_agent.waiter import (
    ReplyWaiter,
    contains_command_approval,
    contains_confirmation,
    contains_interactive_menu,
    is_idle_screen,
    is_ready_screen,
)


class WaiterTests(unittest.TestCase):
    def test_confirmation_detection(self) -> None:
        self.assertTrue(contains_confirmation("Allow this command? [y/n]"))
        self.assertTrue(contains_confirmation("This command requires approval"))
        self.assertTrue(contains_confirmation("Do you want to proceed?"))
        self.assertFalse(contains_confirmation("ordinary output"))

    def test_command_approval_detection(self) -> None:
        text = """
Bash command

   tmux ls 2>&1

 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: tmux ls *
   3. No
"""

        self.assertTrue(contains_command_approval(text))

    def test_interactive_model_menu_detection(self) -> None:
        text = """
  Select model
  Switch between Claude models. Applies to this session only.

    1. Default (recommended)
  ❯ 5. deepseek-v4-pro ✔  Custom model

  Enter to confirm · d to set as default for new sessions · Esc to cancel
"""

        self.assertTrue(contains_interactive_menu(text))

    def test_idle_detection(self) -> None:
        self.assertTrue(is_idle_screen("answer\n> "))
        self.assertTrue(
            is_idle_screen(
                "\n".join(
                    [
                        "answer",
                        "────────────────",
                        '❯ Try "refactor <filepath>"',
                        "────────────────",
                        "  ? for shortcuts        ⧉ In .env",
                    ]
                )
            )
        )
        self.assertFalse(is_idle_screen("answer\nstill running"))
        self.assertFalse(is_idle_screen("❯ 帮我安装 tmux 并启动试试"))

    def test_ready_detection_accepts_dirty_input_line(self) -> None:
        self.assertTrue(is_ready_screen("❯ 帮我安装 tmux 并启动试试"))
        self.assertFalse(
            is_ready_screen(
                "This command requires approval\nDo you want to proceed?\n❯ 1. Yes"
            )
        )

    def test_wait_returns_done_after_stable_prompt(self) -> None:
        screens = iter(["before\nanswer\n> ", "before\nanswer\n> "])
        times = iter([0, 1, 2, 3])
        waiter = ReplyWaiter(
            lambda: next(screens),
            poll_interval=0.1,
            stable_polls=2,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )

        event = waiter.wait("before\n> ", timeout=10)

        self.assertEqual(event.kind, "done")
        self.assertEqual(event.text, "answer\n> ")

    def test_wait_returns_confirmation(self) -> None:
        screens = iter(["before\nAllow this command? [y/n]"])
        times = iter([0, 1])
        waiter = ReplyWaiter(
            lambda: next(screens),
            poll_interval=0.1,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )

        event = waiter.wait("before\n> ", timeout=10)

        self.assertEqual(event.kind, "confirmation")
        self.assertIn("[y/n]", event.prompt)

    def test_wait_returns_command_approval_from_full_screen(self) -> None:
        screen = """
Bash command

 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: tmux ls *
   3. No
"""
        screens = iter([screen])
        times = iter([0, 1])
        waiter = ReplyWaiter(
            lambda: next(screens),
            poll_interval=0.1,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )

        event = waiter.wait("same-old-screen", timeout=10)

        self.assertEqual(event.kind, "confirmation")
        self.assertIn("Do you want to proceed?", event.text)

    def test_wait_ignores_stale_command_approval_from_scrollback(self) -> None:
        baseline = """
Bash command

 This command requires approval

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don’t ask again for: tmux ls *
   3. No

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
"""
        screen = (
            baseline
            + """
❯ hello

● Fresh answer from Claude.

  Thought for 1s (ctrl+o to expand)

✻ Brewed for 3s

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts
"""
        )
        screens = iter([screen, screen])
        times = iter([0, 1, 2, 3])
        waiter = ReplyWaiter(
            lambda: next(screens),
            poll_interval=0.1,
            stable_polls=2,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )

        event = waiter.wait(baseline, timeout=10)

        self.assertEqual(event.kind, "done")
        self.assertIn("Fresh answer from Claude.", event.text)

    def test_wait_returns_interactive_model_menu(self) -> None:
        screen = """
────────────────────────────────────────────────────────────────────────────────
  Select model
  Switch between Claude models. Applies to this session only.

    1. Default (recommended)
  ❯ 5. deepseek-v4-pro ✔  Custom model

  Enter to confirm · d to set as default for new sessions · Esc to cancel
"""
        screens = iter([screen, screen])
        times = iter([0, 1, 2, 3])
        waiter = ReplyWaiter(
            lambda: next(screens),
            poll_interval=0.1,
            stable_polls=2,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )

        event = waiter.wait("previous prompt", timeout=10)

        self.assertEqual(event.kind, "interactive")
        self.assertIn("Select model", event.text)

    def test_wait_until_idle_waits_for_stable_prompt_with_footer(self) -> None:
        screen = "\n".join(
            [
                "Claude Code v2",
                "────────────────",
                '❯ Try "refactor <filepath>"',
                "────────────────",
                "  ? for shortcuts        ⧉ In .env",
            ]
        )
        screens = iter([screen, screen])
        times = iter([0, 1, 2, 3])
        waiter = ReplyWaiter(
            lambda: next(screens),
            poll_interval=0.1,
            stable_polls=2,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )

        event = waiter.wait_until_idle(timeout=10)

        self.assertEqual(event.kind, "ready")

    def test_wait_until_ready_accepts_dirty_prompt_with_footer(self) -> None:
        screen = "\n".join(
            [
                "previous answer",
                "────────────────",
                "❯ 帮我安装 tmux 并启动试试",
                "────────────────",
                "  ? for shortcuts        ⧉ In .env",
            ]
        )
        screens = iter([screen, screen])
        times = iter([0, 1, 2, 3])
        waiter = ReplyWaiter(
            lambda: next(screens),
            poll_interval=0.1,
            stable_polls=2,
            sleep=lambda _: None,
            clock=lambda: next(times),
        )

        event = waiter.wait_until_ready(timeout=10)

        self.assertEqual(event.kind, "ready")


if __name__ == "__main__":
    unittest.main()
