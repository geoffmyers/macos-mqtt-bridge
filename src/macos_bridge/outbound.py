"""Outbound message handling via Messages.app scripting dictionary.

Subscribes to <prefix>/<host>/comms/messages/send. On receipt, runs `osascript`
against Messages.app's scripting dictionary (NOT UI scripting — the
dictionary is a stable Apple-defined interface), sends the message, and
emits the result on <prefix>/<host>/comms/messages/send_result.

Expected inbound payload:
    {
        "to": "+16125550123",            # phone or email
        "service": "iMessage",           # or "SMS"
        "text": "hello world",
        "request_id": "optional-uuid"    # echoed in result so callers can correlate
    }

Outbound result payload:
    {
        "request_id": "<echoed>",
        "to": "<echoed>",
        "service": "<echoed>",
        "success": true | false,
        "error": null | "<message>"
    }
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger(__name__)

_OSASCRIPT_TIMEOUT_SECONDS = 15

_APPLESCRIPT_TEMPLATE = """\
on run argv
    set theService to item 1 of argv
    set theBuddy to item 2 of argv
    set theText to item 3 of argv
    tell application "Messages"
        if theService is "SMS" then
            set theType to SMS
        else
            set theType to iMessage
        end if
        set targetService to 1st account whose service type is theType
        set targetBuddy to participant theBuddy of targetService
        send theText to targetBuddy
    end tell
    return "ok"
end run
"""


@dataclass
class SendRequest:
    to: str
    service: str
    text: str
    request_id: str | None = None

    @classmethod
    def parse(cls, payload_bytes: bytes) -> SendRequest:
        data = json.loads(payload_bytes)
        if not isinstance(data, dict):
            raise ValueError("payload must be a JSON object")
        if not data.get("to"):
            raise ValueError("missing 'to'")
        if not data.get("text"):
            raise ValueError("missing 'text'")
        service = (data.get("service") or "iMessage").strip()
        if service not in {"iMessage", "SMS"}:
            raise ValueError(f"service must be iMessage or SMS, got {service!r}")
        return cls(
            to=str(data["to"]).strip(),
            service=service,
            text=str(data["text"]),
            request_id=data.get("request_id"),
        )


class OutboundHandler:
    def __init__(self, *, osascript_path: str = "osascript"):
        self.osascript_path = shutil.which(osascript_path) or osascript_path

    def send(self, req: SendRequest) -> dict:
        try:
            result = subprocess.run(
                [
                    self.osascript_path, "-e", _APPLESCRIPT_TEMPLATE, "-",
                    req.service, req.to, req.text,
                ],
                capture_output=True,
                text=True,
                timeout=_OSASCRIPT_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError:
            return self._result(req, ok=False, error=f"osascript not found at {self.osascript_path}")
        except subprocess.TimeoutExpired:
            return self._result(req, ok=False, error="osascript timed out")
        except Exception as e:  # noqa: BLE001
            return self._result(req, ok=False, error=f"unexpected error: {e}")

        if result.returncode != 0:
            err = (result.stderr or "").strip() or f"osascript exit {result.returncode}"
            return self._result(req, ok=False, error=err)

        return self._result(req, ok=True, error=None)

    @staticmethod
    def _result(req: SendRequest, *, ok: bool, error: str | None) -> dict:
        return {
            "request_id": req.request_id,
            "to": req.to,
            "service": req.service,
            "success": ok,
            "error": error,
        }
