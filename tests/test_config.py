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

    def test_rejects_invalid_forward_env_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            env = self.make_base_env(path)
            env["CLAUDE_FORWARD_ENV_VARS"] = "HTTPS_PROXY,bad-name"

            with self.assertRaises(ConfigError):
                AppConfig.from_env(env, cwd=path)


if __name__ == "__main__":
    unittest.main()
