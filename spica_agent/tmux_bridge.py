from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping

from .config import AppConfig
from .text_utils import clean_terminal_text


class TmuxError(RuntimeError):
    """Raised for tmux lifecycle, input, or capture failures."""


RunCallable = Callable[..., subprocess.CompletedProcess[str]]
WhichCallable = Callable[[str], str | None]


class TmuxBridge:
    def __init__(
        self,
        config: AppConfig,
        claude_env: Mapping[str, str],
        *,
        run: RunCallable = subprocess.run,
        which: WhichCallable = shutil.which,
    ) -> None:
        self.config = config
        self._claude_env = dict(claude_env)
        self._run_command = run
        self._which = which
        self._tmux = "tmux"

    @property
    def target(self) -> str:
        return self.config.claude_tmux_session

    def check_prerequisites(self) -> None:
        missing: list[str] = []
        if not self._which("tmux"):
            missing.append("tmux")
        if not self._command_exists(self.config.claude_binary):
            missing.append(self.config.claude_binary)
        if not self.config.claude_workdir.is_dir():
            raise TmuxError(f"CLAUDE_WORKDIR does not exist: {self.config.claude_workdir}")
        if not self.config.claude_env_file.is_file():
            raise TmuxError(f"CLAUDE_ENV_FILE does not exist: {self.config.claude_env_file}")
        if missing:
            raise TmuxError("Missing required command(s): " + ", ".join(missing))

    def ensure_session(self) -> None:
        self.check_prerequisites()
        if not self.has_session():
            self._create_session()
        self._set_history_limit()

    def restart_session(self) -> None:
        self.check_prerequisites()
        if self.has_session():
            self._run([self._tmux, "kill-session", "-t", self.target])
        self._create_session()
        self._set_history_limit()

    def has_session(self) -> bool:
        result = self._run(
            [self._tmux, "has-session", "-t", self.target],
            check=False,
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        if result.returncode == 1:
            return False
        raise TmuxError("Failed to inspect tmux session state")

    def capture_screen(self) -> str:
        start_line = f"-{self.config.claude_capture_lines}"
        result = self._run(
            [
                self._tmux,
                "capture-pane",
                "-p",
                "-S",
                start_line,
                "-E",
                "-",
                "-t",
                self.target,
            ],
            capture_output=True,
        )
        return clean_terminal_text(result.stdout)

    def send_text(self, text: str) -> None:
        for chunk in _text_chunks(text):
            self._run(
                [self._tmux, "send-keys", "-t", self.target, "-l", "--", chunk]
            )
        self._run([self._tmux, "send-keys", "-t", self.target, "C-m"])

    def clear_input(self) -> None:
        self._run([self._tmux, "send-keys", "-t", self.target, "C-u"])

    def send_ctrl_c(self) -> None:
        self._run([self._tmux, "send-keys", "-t", self.target, "C-c"])

    def send_key(self, key: str) -> None:
        self._run([self._tmux, "send-keys", "-t", self.target, key])

    def send_keys(self, *keys: str) -> None:
        for key in keys:
            self.send_key(key)

    def _create_session(self) -> None:
        args = [
            self._tmux,
            "new-session",
            "-d",
            "-s",
            self.target,
            "-c",
            str(self.config.claude_workdir),
        ]
        for key, value in sorted(self._claude_env.items()):
            args.extend(["-e", f"{key}={value}"])
        args.append(shlex.join(self.config.claude_command))
        self._run(args)

    def _set_history_limit(self) -> None:
        self._run(
            [
                self._tmux,
                "set-option",
                "-t",
                self.target,
                "history-limit",
                str(self.config.claude_history_limit),
            ]
        )

    def _command_exists(self, command: str) -> bool:
        expanded = Path(command).expanduser()
        if "/" in command:
            return expanded.is_file() and expanded.stat().st_mode & 0o111 != 0
        return bool(self._which(command))

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = self._run_command(
            args,
            text=True,
            input=input,
            capture_output=capture_output,
        )
        if check and result.returncode != 0:
            command_label = " ".join(args[:2])
            stderr = (result.stderr or "").strip()
            detail = f": {stderr}" if stderr else ""
            raise TmuxError(f"tmux command failed ({command_label}){detail}")
        return result


def _text_chunks(text: str, *, size: int = 800) -> list[str]:
    if not text:
        return [""]
    return [text[index : index + size] for index in range(0, len(text), size)]
