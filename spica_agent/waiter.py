from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

from .text_utils import (
    clean_terminal_text,
    extract_new_text,
    is_claude_input_line,
    is_claude_prompt_line,
)


CONFIRMATION_PATTERNS = (
    re.compile(r"Allow this command\?", re.IGNORECASE),
    re.compile(r"requires approval", re.IGNORECASE),
    re.compile(r"Do you want to proceed\?", re.IGNORECASE),
    re.compile(r"Yes,\s+and\s+don[’']t\s+ask\s+again", re.IGNORECASE),
    re.compile(r"^\s*[>❯]\s*1\.\s*Yes\b", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\[[Yy]/[Nn]\]"),
    re.compile(r"\([Yy]/[Nn]\)"),
    re.compile(r"\bProceed\?\s*(?:\[[Yy]/[Nn]\]|\([Yy]/[Nn]\))?", re.IGNORECASE),
)

COMMAND_APPROVAL_PATTERNS = (
    re.compile(r"requires approval", re.IGNORECASE),
    re.compile(r"Do you want to proceed\?", re.IGNORECASE),
    re.compile(r"Yes,\s+and\s+don[’']t\s+ask\s+again", re.IGNORECASE),
    re.compile(r"^\s*[>❯]\s*1\.\s*Yes\b", re.IGNORECASE | re.MULTILINE),
)

INTERACTIVE_MENU_PATTERNS = (
    re.compile(r"^\s*Select model\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"Switch between Claude models", re.IGNORECASE),
    re.compile(r"Enter to confirm", re.IGNORECASE),
    re.compile(r"Esc to cancel", re.IGNORECASE),
    re.compile(r"^\s*[>❯]\s*\d+\.\s+", re.MULTILINE),
)

IDLE_PROMPT_PATTERNS = (
    re.compile(r"^\s*[>❯]\s*$"),
    re.compile(r"^\s*>\s*$"),
)


@dataclass(frozen=True)
class ReplyEvent:
    kind: str
    text: str
    screen: str
    prompt: str = ""


def contains_confirmation(text: str) -> bool:
    return any(pattern.search(text) for pattern in CONFIRMATION_PATTERNS)


def contains_command_approval(text: str) -> bool:
    return any(pattern.search(text) for pattern in COMMAND_APPROVAL_PATTERNS)


def contains_interactive_menu(text: str) -> bool:
    return any(pattern.search(text) for pattern in INTERACTIVE_MENU_PATTERNS)


def find_confirmation_prompt(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines[-20:]):
        if contains_confirmation(line):
            return line
    return lines[-1] if lines else "Claude is waiting for y/n confirmation."


def is_idle_screen(screen: str) -> bool:
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    for line in reversed(lines[-12:]):
        if contains_confirmation(line):
            return False
        if is_claude_prompt_line(line) or any(
            pattern.match(line) for pattern in IDLE_PROMPT_PATTERNS
        ):
            return True
    return False


def is_ready_screen(screen: str) -> bool:
    if contains_confirmation(screen):
        return False
    lines = [line.strip() for line in screen.splitlines() if line.strip()]
    for line in reversed(lines[-12:]):
        if is_claude_input_line(line):
            return True
    return False


class ReplyWaiter:
    def __init__(
        self,
        capture_screen: Callable[[], str],
        *,
        poll_interval: float = 1.0,
        stable_polls: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capture_screen = capture_screen
        self._poll_interval = poll_interval
        self._stable_polls = stable_polls
        self._sleep = sleep
        self._clock = clock

    def wait(self, baseline_screen: str, timeout: int) -> ReplyEvent:
        start = self._clock()
        last_screen: str | None = None
        stable_count = 0
        interactive_stable_count = 0

        while self._clock() - start < timeout:
            self._sleep(self._poll_interval)
            screen = clean_terminal_text(self._capture_screen())
            delta = extract_new_text(baseline_screen, screen)

            confirmation_text = delta
            if not contains_confirmation(confirmation_text) and contains_confirmation(screen):
                confirmation_text = screen

            if contains_confirmation(confirmation_text):
                return ReplyEvent(
                    kind="confirmation",
                    text=confirmation_text,
                    screen=screen,
                    prompt=find_confirmation_prompt(confirmation_text),
                )

            if contains_interactive_menu(screen):
                interactive_stable_count = (
                    interactive_stable_count + 1 if screen == last_screen else 1
                )
                if interactive_stable_count >= self._stable_polls:
                    return ReplyEvent(kind="interactive", text=delta or screen, screen=screen)
            else:
                interactive_stable_count = 0

            if is_idle_screen(screen):
                stable_count = stable_count + 1 if screen == last_screen else 1
                if stable_count >= self._stable_polls:
                    return ReplyEvent(kind="done", text=delta, screen=screen)
            else:
                stable_count = 0

            last_screen = screen

        screen = clean_terminal_text(self._capture_screen())
        return ReplyEvent(
            kind="timeout",
            text=extract_new_text(baseline_screen, screen),
            screen=screen,
        )

    def wait_until_idle(self, timeout: int) -> ReplyEvent:
        return self._wait_for_screen(timeout, is_idle_screen)

    def wait_until_ready(self, timeout: int) -> ReplyEvent:
        return self._wait_for_screen(timeout, is_ready_screen)

    def _wait_for_screen(
        self, timeout: int, predicate: Callable[[str], bool]
    ) -> ReplyEvent:
        start = self._clock()
        last_screen: str | None = None
        stable_count = 0

        while self._clock() - start < timeout:
            self._sleep(self._poll_interval)
            screen = clean_terminal_text(self._capture_screen())
            if predicate(screen):
                stable_count = stable_count + 1 if screen == last_screen else 1
                if stable_count >= self._stable_polls:
                    return ReplyEvent(kind="ready", text="", screen=screen)
            else:
                stable_count = 0
            last_screen = screen

        screen = clean_terminal_text(self._capture_screen())
        return ReplyEvent(kind="timeout", text="", screen=screen)
