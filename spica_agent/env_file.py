from __future__ import annotations

import re
import shlex
from pathlib import Path


class EnvFileError(ValueError):
    """Raised when the Claude environment file cannot be parsed safely."""


_ASSIGNMENT_RE = re.compile(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")


def _parse_value(raw_value: str, line_number: int, key: str) -> str:
    value = raw_value.strip()
    if value == "":
        return ""

    lexer = shlex.shlex(value, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError as exc:
        raise EnvFileError(
            f"Invalid quoting in environment file at line {line_number} for {key}"
        ) from exc

    if len(tokens) != 1:
        raise EnvFileError(
            f"Unsupported value syntax in environment file at line {line_number} for {key}"
        )
    return tokens[0]


def load_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        raise EnvFileError(f"Environment file does not exist: {env_path}")

    values: dict[str, str] = {}
    content = env_path.read_text(encoding="utf-8")
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        match = _ASSIGNMENT_RE.fullmatch(line)
        if not match:
            raise EnvFileError(
                f"Unsupported environment file syntax at line {line_number}"
            )

        key, raw_value = match.groups()
        values[key] = _parse_value(raw_value, line_number, key)

    return values
