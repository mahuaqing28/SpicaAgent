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


def _parse_extensions(raw: str) -> frozenset[str]:
    extensions: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        value = part.strip().lower()
        if not value:
            continue
        if not value.startswith(".") or "/" in value or "\\" in value:
            raise ConfigError("SPICA_FILE_ALLOWED_EXTENSIONS contains an invalid extension")
        extensions.add(value)
    if not extensions:
        raise ConfigError("SPICA_FILE_ALLOWED_EXTENSIONS must not be empty")
    return frozenset(extensions)


def _parse_paths(raw: str) -> tuple[Path, ...]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for part in raw.replace(";", ",").split(","):
        value = part.strip()
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if path not in seen:
            paths.append(path)
            seen.add(path)
    return tuple(paths)


def _parse_strings(raw: str) -> frozenset[str]:
    values: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        value = part.strip()
        if value:
            values.add(value)
    return frozenset(values)


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
    phone_bridge_enabled: bool
    phone_bridge_host: str
    phone_bridge_port: int
    phone_bridge_token: str
    phone_notify_chat_ids: frozenset[int]
    spica_files_enabled: bool
    spica_file_root: Path
    spica_file_output_roots: tuple[Path, ...]
    spica_file_allowed_extensions: frozenset[str]
    spica_file_max_upload_mb: int
    schedule_bridge_enabled: bool
    schedule_bridge_token: str
    schedule_state_file: Path
    schedule_stateshare_file: Path | None
    schedule_agent_dir: Path
    schedule_agent_history_days: int
    schedule_non_work_packages: frozenset[str]
    schedule_non_work_threshold_minutes: int
    schedule_reminder_cooldown_minutes: int
    schedule_reminder_check_interval_seconds: int

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

        phone_bridge_enabled = _parse_bool(source, "PHONE_BRIDGE_ENABLED", False)
        phone_bridge_token = source.get("PHONE_BRIDGE_TOKEN", "").strip()
        if phone_bridge_enabled and not phone_bridge_token:
            raise ConfigError("PHONE_BRIDGE_TOKEN is required when PHONE_BRIDGE_ENABLED=true")

        schedule_bridge_enabled = _parse_bool(source, "SCHEDULE_BRIDGE_ENABLED", False)
        schedule_bridge_token = source.get("SCHEDULE_BRIDGE_TOKEN", phone_bridge_token).strip()
        if schedule_bridge_enabled and not schedule_bridge_token:
            raise ConfigError(
                "SCHEDULE_BRIDGE_TOKEN or PHONE_BRIDGE_TOKEN is required when "
                "SCHEDULE_BRIDGE_ENABLED=true"
            )

        claude_workdir = Path(
            source.get("CLAUDE_WORKDIR", str(current_dir))
        ).expanduser().resolve()
        file_root = Path(source.get("SPICA_FILE_ROOT", "/tmp/spica-agent")).expanduser().resolve()
        output_roots_raw = source.get(
            "SPICA_FILE_OUTPUT_ROOTS",
            f"{claude_workdir},{file_root / 'outputs'}",
        )
        state_share_raw = source.get("SCHEDULE_STATESHARE_FILE", "").strip()

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
            claude_workdir=claude_workdir,
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
            phone_bridge_enabled=phone_bridge_enabled,
            phone_bridge_host=source.get("PHONE_BRIDGE_HOST", "0.0.0.0").strip()
            or "0.0.0.0",
            phone_bridge_port=_parse_int(
                source, "PHONE_BRIDGE_PORT", 8765, minimum=1
            ),
            phone_bridge_token=phone_bridge_token,
            phone_notify_chat_ids=_parse_chat_ids(
                source.get("PHONE_NOTIFY_CHAT_IDS", "")
            ),
            spica_files_enabled=_parse_bool(source, "SPICA_FILES_ENABLED", False),
            spica_file_root=file_root,
            spica_file_output_roots=_parse_paths(output_roots_raw),
            spica_file_allowed_extensions=_parse_extensions(
                source.get(
                    "SPICA_FILE_ALLOWED_EXTENSIONS",
                    ".png,.jpg,.jpeg,.webp,.gif,.pdf,.txt,.md,.zip",
                )
            ),
            spica_file_max_upload_mb=_parse_int(
                source, "SPICA_FILE_MAX_UPLOAD_MB", 50, minimum=1
            ),
            schedule_bridge_enabled=schedule_bridge_enabled,
            schedule_bridge_token=schedule_bridge_token,
            schedule_state_file=Path(
                source.get("SCHEDULE_STATE_FILE", "/tmp/spica-agent/schedule-state.json")
            )
            .expanduser()
            .resolve(),
            schedule_stateshare_file=(
                Path(state_share_raw).expanduser().resolve() if state_share_raw else None
            ),
            schedule_agent_dir=Path(
                source.get("SCHEDULE_AGENT_DIR", str(claude_workdir / "schedule"))
            )
            .expanduser()
            .resolve(),
            schedule_agent_history_days=_parse_int(
                source, "SCHEDULE_AGENT_HISTORY_DAYS", 7, minimum=1
            ),
            schedule_non_work_packages=_parse_strings(
                source.get("SCHEDULE_NON_WORK_PACKAGES", "")
            ),
            schedule_non_work_threshold_minutes=_parse_int(
                source, "SCHEDULE_NON_WORK_THRESHOLD_MINUTES", 20, minimum=1
            ),
            schedule_reminder_cooldown_minutes=_parse_int(
                source, "SCHEDULE_REMINDER_COOLDOWN_MINUTES", 120, minimum=1
            ),
            schedule_reminder_check_interval_seconds=_parse_int(
                source, "SCHEDULE_REMINDER_CHECK_INTERVAL_SECONDS", 60, minimum=1
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
