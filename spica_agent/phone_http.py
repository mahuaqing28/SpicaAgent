from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .phone import PhonePayloadError, PhoneStateStore
from .schedule import SchedulePayloadError, ScheduleReminder, ScheduleStateStore
from .telegram import TelegramClient


LOGGER = logging.getLogger(__name__)


class PhoneHttpServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        store: PhoneStateStore | None = None,
        telegram: TelegramClient,
        notify_chat_ids: frozenset[int],
        schedule_store: ScheduleStateStore | None = None,
        schedule_token: str | None = None,
        schedule_reminder_callback: Callable[[int, ScheduleReminder], None] | None = None,
    ) -> None:
        self._server = ThreadingHTTPServer(
            (host, port),
            _handler_factory(
                token,
                store,
                telegram,
                notify_chat_ids,
                schedule_store,
                schedule_token or token,
                schedule_reminder_callback,
            ),
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="phone-http",
            daemon=True,
        )

    @property
    def server_address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _handler_factory(
    token: str,
    store: PhoneStateStore | None,
    telegram: TelegramClient,
    notify_chat_ids: frozenset[int],
    schedule_store: ScheduleStateStore | None,
    schedule_token: str,
    schedule_reminder_callback: Callable[[int, ScheduleReminder], None] | None,
) -> type[BaseHTTPRequestHandler]:
    class PhoneRequestHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path in {"/api/schedule/status", "/api/schedule/stateshare"}:
                self._handle_schedule_get()
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path == "/api/phone/events":
                self._handle_phone_events()
                return
            if self.path in {"/api/schedule/snapshot", "/api/schedule/changes"}:
                self._handle_schedule()
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def _handle_phone_events(self) -> None:
            if store is None:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            if self.headers.get("Authorization", "") != f"Bearer {token}":
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            try:
                payload = self._read_json()
                result = store.process_payload(payload)
            except PhonePayloadError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            except ValueError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
                return

            for message in result.notifications:
                for chat_id in notify_chat_ids:
                    try:
                        telegram.send_message(chat_id, message)
                    except Exception:
                        LOGGER.exception("Failed to send phone notification")

            self._send_json(
                200,
                {
                    "ok": True,
                    "accepted_event_ids": result.accepted_event_ids,
                },
            )

        def _handle_schedule(self) -> None:
            if schedule_store is None:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            if self.headers.get("Authorization", "") != f"Bearer {schedule_token}":
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            try:
                payload = self._read_json()
                if self.path == "/api/schedule/snapshot":
                    result = schedule_store.process_snapshot(payload)
                else:
                    result = schedule_store.process_changes(payload)
            except SchedulePayloadError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
                return
            except ValueError:
                self._send_json(400, {"ok": False, "error": "invalid json"})
                return

            reminder_count = 0
            for reminder in result.reminders:
                reminder_count += 1
                for chat_id in notify_chat_ids:
                    try:
                        if schedule_reminder_callback is not None:
                            schedule_reminder_callback(chat_id, reminder)
                        else:
                            telegram.send_message(chat_id, reminder.text)
                    except Exception:
                        LOGGER.exception("Failed to send schedule reminder")

            self._send_json(
                200,
                {
                    "ok": True,
                    "accepted_task_ids": result.accepted_task_ids,
                    "reminder_count": reminder_count,
                },
            )

        def _handle_schedule_get(self) -> None:
            if schedule_store is None:
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            if self.headers.get("Authorization", "") != f"Bearer {schedule_token}":
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            if self.path == "/api/schedule/status":
                payload = schedule_store.status_payload()
            else:
                payload = schedule_store.state_share_payload()
            self._send_json(200, {"ok": True, "data": payload})

        def log_message(self, format: str, *args: Any) -> None:
            LOGGER.info("Bridge HTTP: " + format, *args)

        def _read_json(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError as exc:
                raise ValueError("invalid content length") from exc
            if length <= 0 or length > 1_000_000:
                raise ValueError("invalid content length")
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("payload must be an object")
            return data

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return PhoneRequestHandler
