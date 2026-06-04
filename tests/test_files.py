from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from spica_agent.files import FileStoreError, SpicaFileStore, format_file_list


class FileStoreTests(unittest.TestCase):
    def make_store(self, root: Path, output: Path) -> SpicaFileStore:
        return SpicaFileStore(
            root=root,
            output_roots=(output,),
            allowed_extensions=frozenset({".png", ".txt", ".zip"}),
            max_upload_bytes=1024,
            now=lambda: 1_700_000_000,
        )

    def test_save_upload_stores_under_chat_date_and_tracks_last_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = self.make_store(base / "files", base / "out")

            stored = store.save_upload(
                chat_id=42,
                original_name="../hello.png",
                source="photo",
                content=b"image",
            )

            self.assertTrue(stored.path.is_file())
            self.assertIn("/uploads/42/", str(stored.path))
            self.assertEqual(stored.name, "hello.png")
            last = store.last_for_chat(42)
            self.assertIsNotNone(last)
            assert last is not None
            self.assertEqual(last.id, stored.id)
            self.assertEqual(last.path, stored.path)

    def test_rejects_disallowed_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = self.make_store(base / "files", base / "out")

            with self.assertRaises(FileStoreError):
                store.save_upload(
                    chat_id=42,
                    original_name="secret.exe",
                    source="document",
                    content=b"x",
                )

    def test_rejects_oversized_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = self.make_store(base / "files", base / "out")

            with self.assertRaises(FileStoreError):
                store.save_upload(
                    chat_id=42,
                    original_name="big.zip",
                    source="document",
                    content=b"x" * 1025,
                )

    def test_lists_output_files_but_not_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            output = base / "out"
            output.mkdir()
            allowed = output / "result.txt"
            allowed.write_text("ok", encoding="utf-8")
            outside = base / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            (output / "escape.txt").symlink_to(outside)
            store = self.make_store(base / "files", output)

            files = store.list_recent()
            names = {item.name for item in files}

            self.assertIn("result.txt", names)
            self.assertNotIn("escape.txt", names)
            self.assertIn("/file", format_file_list(files))

    def test_get_rejects_unknown_file_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            store = self.make_store(base / "files", base / "out")

            self.assertIsNone(store.get("missing"))


if __name__ == "__main__":
    unittest.main()
