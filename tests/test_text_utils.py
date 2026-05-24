from __future__ import annotations

import unittest

from spica_agent.text_utils import (
    clean_terminal_text,
    extract_new_text,
    split_telegram_text,
    strip_claude_tui_chrome,
)


class TextUtilsTests(unittest.TestCase):
    def test_clean_terminal_text_removes_ansi(self) -> None:
        self.assertEqual(clean_terminal_text("\x1b[32mhello\x1b[0m\r\n> "), "hello\n> ")

    def test_extract_new_text_prefers_common_prefix(self) -> None:
        before = "old line\n> "
        after = "old line\n> prompt\nanswer\n> "

        self.assertEqual(extract_new_text(before, after), "prompt\nanswer\n> ")

    def test_split_text_respects_limit_and_truncates(self) -> None:
        chunks = split_telegram_text("abc def ghi", limit=7)

        self.assertEqual(chunks, ["abc def", "ghi"])

        truncated = split_telegram_text("x" * 200, limit=120, max_total_chars=100)
        self.assertTrue(
            truncated[-1].endswith("[Output truncated by TELEGRAM_MAX_REPLY_CHARS]")
        )

    def test_strip_claude_tui_chrome_removes_welcome_screen(self) -> None:
        text = """
╭─── Claude Code v2.1.150 ─────────────────────────╮
│                    Welcome back!                 │
│                                                    │ Tips for getting
╰──────────────────────────────────────────────────╯

────────────────────────────────────────────────────
❯ Try "refactor <filepath>"
────────────────────────────────────────────────────
  ? for shortcuts · ← for agents                               ◈ max · /effort
"""

        self.assertEqual(strip_claude_tui_chrome(text), "")

    def test_strip_claude_tui_chrome_removes_real_startup_capture(self) -> None:
        text = """
╭─── Claude Code v2.1.150 ─────────────────────────────────────────────────────╮
│                                                    │ Tips for getting        │
│                    Welcome back!                   │ started                 │
│                                                    │ Run /init to create a … │
│                       ▐▛███▜▌                      │ ─────────────────────── │
│                      ▝▜█████▛▘                     │ What's new              │
│                        ▘▘ ▝▝                       │ Internal infrastructur… │
│                                                    │ `/usage` now shows a p… │
│ deepseek-v4-pro[1m] with max … · API Usage Billing │ `/diff` detail view ca… │
│           ~/personnalProject/SpicaAgent            │ /release-notes for more │
╰──────────────────────────────────────────────────────────────────────────────╯

────────────────────────────────────────────────────────────────────────────────
❯ Try "refactor <filepath>"
────────────────────────────────────────────────────────────────────────────────
  ? for shortcuts · ← for agents                               ◈ max · /effort
"""

        self.assertEqual(strip_claude_tui_chrome(text), "")

    def test_strip_claude_tui_chrome_keeps_answer_text(self) -> None:
        text = """
────────────────────────────────────────────────────
Here is the answer:

```python
print("hello")
```
❯
  ? for shortcuts
"""

        self.assertEqual(
            strip_claude_tui_chrome(text),
            'Here is the answer:\n\n```python\nprint("hello")\n```',
        )


if __name__ == "__main__":
    unittest.main()
