from __future__ import annotations

import re


ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\][^\x07]*(?:\x07|\x1B\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)


def clean_terminal_text(text: str) -> str:
    without_ansi = ANSI_ESCAPE_RE.sub("", text)
    return without_ansi.replace("\r\n", "\n").replace("\r", "\n")


def extract_new_text(before: str, after: str) -> str:
    if not before:
        return after.lstrip("\n")
    if after.startswith(before):
        return after[len(before) :].lstrip("\n")

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    index = 0
    max_index = min(len(before_lines), len(after_lines))
    while index < max_index and before_lines[index] == after_lines[index]:
        index += 1
    return "\n".join(after_lines[index:]).lstrip("\n")


def split_telegram_text(
    text: str, *, limit: int = 3900, max_total_chars: int | None = None
) -> list[str]:
    if max_total_chars is not None and len(text) > max_total_chars:
        text = (
            text[: max(0, max_total_chars - 80)].rstrip()
            + "\n\n[Output truncated by TELEGRAM_MAX_REPLY_CHARS]"
        )

    if not text:
        return ["(empty response)"]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        newline_index = remaining.rfind("\n", 0, limit + 1)
        space_index = remaining.rfind(" ", 0, limit + 1)
        split_at = max(newline_index, space_index)
        if split_at < max(1, limit // 2):
            split_at = limit

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks or ["(empty response)"]


BOX_DRAWING_RE = re.compile(r"^[\s─━═│┃║╭╮╰╯┌┐└┘├┤┬┴┼╔╗╚╝╠╣╦╩╬]+$")
CLAUDE_PROMPT_RE = re.compile(r'^\s*[>❯](?:\s+Try\s+["“].*["”])?\s*$')
CLAUDE_INPUT_RE = re.compile(r"^\s*[>❯](?:\s+.*)?$")
CLAUDE_CHROME_MARKERS = (
    "Claude Code v",
    "Welcome back!",
    "Tips for getting",
    "Run /init",
    "What's new",
    "/release-notes",
    "API Usage Billing",
    "? for shortcuts",
    "⧉ In ",
)


def strip_claude_tui_chrome(text: str) -> str:
    """Remove Claude Code's full-screen TUI chrome from captured pane text."""

    has_welcome_box = "Claude Code v" in text and "Welcome back!" in text
    kept: list[str] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if is_claude_prompt_line(stripped):
            continue
        if BOX_DRAWING_RE.match(stripped):
            continue
        if any(marker in stripped for marker in CLAUDE_CHROME_MARKERS):
            continue
        if has_welcome_box and stripped.startswith("│"):
            continue
        kept.append(line.rstrip())

    return _trim_blank_lines("\n".join(kept))


def is_claude_prompt_line(line: str) -> bool:
    return bool(CLAUDE_PROMPT_RE.match(line.strip()))


def is_claude_input_line(line: str) -> bool:
    stripped = line.strip()
    if re.match(r"^[>❯]\s*\d+\.\s+", stripped):
        return False
    return bool(CLAUDE_INPUT_RE.match(stripped))


def _trim_blank_lines(text: str) -> str:
    lines = text.splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)
