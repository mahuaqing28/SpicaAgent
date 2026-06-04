from __future__ import annotations

import http.client
import json
import mimetypes
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class TelegramError(RuntimeError):
    """Raised when Telegram Bot API returns an error response."""


@dataclass(frozen=True)
class TelegramAttachment:
    kind: str
    file_id: str
    file_unique_id: str
    file_name: str
    file_size: int | None = None
    mime_type: str | None = None


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    chat_id: int
    message_id: int
    text: str
    attachment: TelegramAttachment | None = None


class TelegramClient:
    def __init__(self, token: str, *, api_base: str = "https://api.telegram.org") -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        response = self._request("getUpdates", payload, timeout=timeout + 15)
        return list(response.get("result", []))

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        self._request("sendMessage", payload, timeout=30)

    def get_file_path(self, file_id: str) -> str:
        response = self._request("getFile", {"file_id": file_id}, timeout=30)
        result = response.get("result")
        if not isinstance(result, dict):
            raise TelegramError("Telegram getFile returned invalid result")
        file_path = result.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            raise TelegramError("Telegram getFile response did not include file_path")
        return file_path

    def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        file_path = self.get_file_path(file_id)
        url = f"{self._api_base}/file/bot{self._token}/{file_path}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        if int(content_length) > max_bytes:
                            raise TelegramError("Telegram file is larger than allowed")
                    except ValueError:
                        pass
                data = response.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"Telegram file download HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", "network error")
            raise TelegramError(f"Telegram file download failed: {reason}") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise TelegramError(f"Telegram file download connection failed: {exc}") from exc
        if len(data) > max_bytes:
            raise TelegramError("Telegram file is larger than allowed")
        return data

    def send_photo(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        self._send_file(
            "sendPhoto",
            "photo",
            chat_id,
            path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )

    def send_document(
        self,
        chat_id: int,
        path: Path,
        *,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
    ) -> None:
        self._send_file(
            "sendDocument",
            "document",
            chat_id,
            path,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )

    def parse_message(self, update: dict[str, Any]) -> TelegramMessage | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        caption = message.get("caption")
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        update_id = update.get("update_id")
        if not isinstance(chat_id, int):
            return None
        if not isinstance(message_id, int) or not isinstance(update_id, int):
            return None
        attachment = _parse_attachment(message)
        if not isinstance(text, str):
            text = caption if isinstance(caption, str) else ""
        if not text and attachment is None:
            return None
        return TelegramMessage(
            update_id=update_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            attachment=attachment,
        )

    def _request(
        self, method: str, payload: dict[str, Any], *, timeout: int
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self._api_base}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"Telegram API HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", "network error")
            raise TelegramError(f"Telegram API request failed: {reason}") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise TelegramError(f"Telegram API connection failed: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramError("Telegram API returned invalid JSON") from exc
        if not data.get("ok"):
            description = data.get("description", "unknown Telegram API error")
            raise TelegramError(str(description))
        return data

    def _send_file(
        self,
        method: str,
        field_name: str,
        chat_id: int,
        path: Path,
        *,
        caption: str | None,
        reply_to_message_id: int | None,
    ) -> None:
        fields: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            fields["caption"] = caption
        if reply_to_message_id is not None:
            fields["reply_to_message_id"] = str(reply_to_message_id)

        body, content_type = _multipart_body(fields, field_name, path)
        request = urllib.request.Request(
            f"{self._api_base}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": content_type},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise TelegramError(f"Telegram API HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", "network error")
            raise TelegramError(f"Telegram API request failed: {reason}") from exc
        except (http.client.HTTPException, OSError) as exc:
            raise TelegramError(f"Telegram API connection failed: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TelegramError("Telegram API returned invalid JSON") from exc
        if not data.get("ok"):
            description = data.get("description", "unknown Telegram API error")
            raise TelegramError(str(description))


def _parse_attachment(message: dict[str, Any]) -> TelegramAttachment | None:
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        candidates = [item for item in photo if isinstance(item, dict)]
        if candidates:
            best = max(
                candidates,
                key=lambda item: (
                    _optional_int(item.get("file_size")) or 0,
                    (_optional_int(item.get("width")) or 0)
                    * (_optional_int(item.get("height")) or 0),
                ),
            )
            file_id = best.get("file_id")
            if isinstance(file_id, str) and file_id:
                return TelegramAttachment(
                    kind="photo",
                    file_id=file_id,
                    file_unique_id=str(best.get("file_unique_id") or ""),
                    file_name=f"{file_id}.jpg",
                    file_size=_optional_int(best.get("file_size")),
                    mime_type="image/jpeg",
                )

    document = message.get("document")
    if isinstance(document, dict):
        file_id = document.get("file_id")
        if isinstance(file_id, str) and file_id:
            file_name = document.get("file_name")
            if not isinstance(file_name, str) or not file_name.strip():
                file_name = file_id
            mime_type = document.get("mime_type")
            return TelegramAttachment(
                kind="document",
                file_id=file_id,
                file_unique_id=str(document.get("file_unique_id") or ""),
                file_name=file_name,
                file_size=_optional_int(document.get("file_size")),
                mime_type=mime_type if isinstance(mime_type, str) else None,
            )
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _multipart_body(
    fields: dict[str, str], file_field: str, path: Path
) -> tuple[bytes, str]:
    boundary = "spica-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("utf-8"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{path.name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            path.read_bytes(),
            b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"
