from __future__ import annotations

import unittest

from spica_agent.worker import _normalize_confirmation


class WorkerTests(unittest.TestCase):
    def test_normalize_confirmation_supports_command_approval_choices(self) -> None:
        self.assertEqual(_normalize_confirmation("/approve"), "approve")
        self.assertEqual(_normalize_confirmation("1"), "approve")
        self.assertEqual(_normalize_confirmation("/approve_always"), "approve_always")
        self.assertEqual(_normalize_confirmation("2"), "approve_always")
        self.assertEqual(_normalize_confirmation("/deny"), "deny")
        self.assertEqual(_normalize_confirmation("3"), "deny")


if __name__ == "__main__":
    unittest.main()
