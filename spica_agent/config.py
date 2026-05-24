from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """Raised when environment-based configuration is invalid."""


def _parse_chat_ids(raw: str) -> frozenset[int]:
    chat_ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        value = part.strip()
        if not value:
            continue
        try:
            chat_ids.add(int(value))
        except ValueError as exc:
            raise ConfigError(
                "TELEGRAM_ALLOWED_CHAT_IDS must contain comma-separated integers"
            ) from exc
    return frozenset(chat_ids)


def _parse_int(
    env: Mapping[str, str], name: str, default: int, *, minimum: int = 1
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _parse_float(
    env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.1
) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str
    telegram_allowed_chat_ids: frozenset[int]
    telegram_api_base: str
    telegram_poll_timeout: int
    telegram_drop_pending_updates: bool
    telegram_message_limit: int
    telegram_max_reply_chars: int
    claude_workdir: Path
    claude_tmux_session: str
    claude_command: tuple[str, ...]
    claude_env_file: Path
    claude_forward_env_vars: tuple[str, ...]
    claude_ready_timeout: int
    claude_reply_timeout: int
    claude_confirm_timeout: int
    claude_poll_interval: float
    claude_history_limit: int
    claude_capture_lines: int

    @property
    def claude_binary(self) -> str:
        return self.claude_command[0]

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
    ) -> "AppConfig":
        source = os.environ if env is None else env
        current_dir = Path(cwd or os.getcwd()).expanduser().resolve()

        token = source.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required")

        api_base = source.get("TELEGRAM_API_BASE", "https://api.telegram.org").strip()
        if not api_base:
            raise ConfigError("TELEGRAM_API_BASE must not be empty")

        command_raw = source.get("CLAUDE_COMMAND", "claude").strip()
        try:
            command = tuple(shlex.split(command_raw))
        except ValueError as exc:
            raise ConfigError("CLAUDE_COMMAND has invalid shell quoting") from exc
        if not command:
            raise ConfigError("CLAUDE_COMMAND must not be empty")

        message_limit = _parse_int(
            source, "TELEGRAM_MAX_MESSAGE_CHARS", 3900, minimum=100
        )
        if message_limit > 4096:
            raise ConfigError("TELEGRAM_MAX_MESSAGE_CHARS must be <= 4096")

        return cls(
            telegram_bot_token=token,
            telegram_allowed_chat_ids=_parse_chat_ids(
                source.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
            ),
            telegram_api_base=api_base.rstrip("/"),
            telegram_poll_timeout=_parse_int(
                source, "TELEGRAM_POLL_TIMEOUT", 20, minimum=1
            ),
            telegram_drop_pending_updates=_parse_bool(
                source, "TELEGRAM_DROP_PENDING_UPDATES", True
            ),
            telegram_message_limit=message_limit,
            telegram_max_reply_chars=_parse_int(
                source, "TELEGRAM_MAX_REPLY_CHARS", 60000, minimum=1000
            ),
            claude_workdir=Path(
                source.get("CLAUDE_WORKDIR", str(current_dir))
            )
            .expanduser()
            .resolve(),
            claude_tmux_session=source.get("CLAUDE_TMUX_SESSION", "claude_bg").strip()
            or "claude_bg",
            claude_command=command,
            claude_env_file=Path(
                source.get("CLAUDE_ENV_FILE", "/home/mahuaqing/config/deepseek.txt")
            )
            .expanduser()
            .resolve(),
            claude_forward_env_vars=_parse_env_var_names(
                source.get(
                    "CLAUDE_FORWARD_ENV_VARS",
                    ",".join(
                        [
                            "HTTP_PROXY",
                            "HTTPS_PROXY",
                            "ALL_PROXY",
                            "NO_PROXY",
                            "http_proxy",
                            "https_proxy",
                            "all_proxy",
                            "no_proxy",
                        ]
                    ),
                )
            ),
            claude_ready_timeout=_parse_int(
                source, "CLAUDE_READY_TIMEOUT", 120, minimum=5
            ),
            claude_reply_timeout=_parse_int(
                source, "CLAUDE_REPLY_TIMEOUT", 600, minimum=5
            ),
            claude_confirm_timeout=_parse_int(
                source, "CLAUDE_CONFIRM_TIMEOUT", 300, minimum=5
            ),
            claude_poll_interval=_parse_float(
                source, "CLAUDE_POLL_INTERVAL", 1.0, minimum=0.1
            ),
            claude_history_limit=_parse_int(
                source, "CLAUDE_HISTORY_LIMIT", 100000, minimum=2000
            ),
            claude_capture_lines=_parse_int(
                source, "CLAUDE_CAPTURE_LINES", 2000, minimum=200
            ),
        )


def _parse_env_var_names(raw: str) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        name = part.strip()
        if not name:
            continue
        if not name.replace("_", "A").isalnum() or name[0].isdigit():
            raise ConfigError("CLAUDE_FORWARD_ENV_VARS contains an invalid env var name")
        if name not in seen:
            names.append(name)
            seen.add(name)
    return tuple(names)
