from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class TelegramError(RuntimeError):
    """Raised when Telegram Bot API returns an error response."""


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    chat_id: int
    message_id: int
    text: str


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

    def parse_message(self, update: dict[str, Any]) -> TelegramMessage | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        text = message.get("text")
        chat = message.get("chat")
        if not isinstance(text, str) or not isinstance(chat, dict):
            return None
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        update_id = update.get("update_id")
        if not isinstance(chat_id, int):
            return None
        if not isinstance(message_id, int) or not isinstance(update_id, int):
            return None
        return TelegramMessage(
            update_id=update_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
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
