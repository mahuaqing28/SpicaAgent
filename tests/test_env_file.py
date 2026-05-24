from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spica_agent.env_file import EnvFileError, load_env_file


class EnvFileTests(unittest.TestCase):
    def write_env(self, text: str) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "env.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_export_and_assignment_lines(self) -> None:
        path = self.write_env(
            """
            # comment
            export API_KEY=abc123
            BASE_URL="https://example.test/v1"
            MODEL='deep seek'
            EMPTY=
            """
        )

        self.assertEqual(
            load_env_file(path),
            {
                "API_KEY": "abc123",
                "BASE_URL": "https://example.test/v1",
                "MODEL": "deep seek",
                "EMPTY": "",
            },
        )

    def test_rejects_unsupported_shell_syntax_without_leaking_value(self) -> None:
        path = self.write_env("export BAD-NAME=super-secret-token\n")

        with self.assertRaises(EnvFileError) as ctx:
            load_env_file(path)

        self.assertIn("line 1", str(ctx.exception))
        self.assertNotIn("super-secret-token", str(ctx.exception))

    def test_rejects_unquoted_spaces(self) -> None:
        path = self.write_env("export API_KEY=foo bar\n")

        with self.assertRaises(EnvFileError):
            load_env_file(path)


if __name__ == "__main__":
    unittest.main()
