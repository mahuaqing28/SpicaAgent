from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from spica_agent.telegram import TelegramClient


class TelegramClientTests(unittest.TestCase):
    def test_parse_text_message(self) -> None:
        client = TelegramClient("token")

        message = client.parse_message(
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "chat": {"id": 42},
                    "text": "hello",
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.text, "hello")
        self.assertIsNone(message.attachment)

    def test_parse_photo_message_uses_caption_and_largest_photo(self) -> None:
        client = TelegramClient("token")

        message = client.parse_message(
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "chat": {"id": 42},
                    "caption": "use this",
                    "photo": [
                        {
                            "file_id": "small",
                            "file_unique_id": "s",
                            "width": 90,
                            "height": 90,
                            "file_size": 100,
                        },
                        {
                            "file_id": "large",
                            "file_unique_id": "l",
                            "width": 900,
                            "height": 900,
                            "file_size": 1000,
                        },
                    ],
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.text, "use this")
        self.assertIsNotNone(message.attachment)
        assert message.attachment is not None
        self.assertEqual(message.attachment.kind, "photo")
        self.assertEqual(message.attachment.file_id, "large")
        self.assertEqual(message.attachment.file_name, "large.jpg")

    def test_parse_document_message(self) -> None:
        client = TelegramClient("token")

        message = client.parse_message(
            {
                "update_id": 1,
                "message": {
                    "message_id": 7,
                    "chat": {"id": 42},
                    "document": {
                        "file_id": "doc-file",
                        "file_unique_id": "doc-unique",
                        "file_name": "report.pdf",
                        "file_size": 123,
                        "mime_type": "application/pdf",
                    },
                },
            }
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual(message.text, "")
        self.assertIsNotNone(message.attachment)
        assert message.attachment is not None
        self.assertEqual(message.attachment.kind, "document")
        self.assertEqual(message.attachment.file_name, "report.pdf")

    def test_download_file_calls_get_file_and_downloads_file_url(self) -> None:
        client = TelegramClient("token", api_base="https://api.telegram.test")
        client.get_file_path = MagicMock(return_value="photos/file.jpg")  # type: ignore[method-assign]
        fake_response = MagicMock()
        fake_response.__enter__.return_value.read.return_value = b"image"
        fake_response.__enter__.return_value.headers = {"Content-Length": "5"}

        with patch("urllib.request.urlopen", return_value=fake_response) as urlopen:
            data = client.download_file("file-id", max_bytes=10)

        self.assertEqual(data, b"image")
        client.get_file_path.assert_called_once_with("file-id")  # type: ignore[attr-defined]
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.telegram.test/file/bottoken/photos/file.jpg")


if __name__ == "__main__":
    unittest.main()
