from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from macos_bridge.outbound import OutboundHandler, SendRequest


def test_parse_valid_request():
    payload = json.dumps({"to": "+16125550123", "text": "hi"}).encode()
    req = SendRequest.parse(payload)
    assert req.to == "+16125550123"
    assert req.text == "hi"
    assert req.service == "iMessage"  # default
    assert req.request_id is None


def test_parse_with_sms_and_request_id():
    payload = json.dumps(
        {"to": "+16125550123", "text": "hi", "service": "SMS", "request_id": "abc"}
    ).encode()
    req = SendRequest.parse(payload)
    assert req.service == "SMS"
    assert req.request_id == "abc"


def test_parse_rejects_missing_to():
    with pytest.raises(ValueError, match="'to'"):
        SendRequest.parse(json.dumps({"text": "hi"}).encode())


def test_parse_rejects_missing_text():
    with pytest.raises(ValueError, match="'text'"):
        SendRequest.parse(json.dumps({"to": "+16125550123"}).encode())


def test_parse_rejects_invalid_service():
    with pytest.raises(ValueError, match="service"):
        SendRequest.parse(json.dumps({"to": "x", "text": "y", "service": "Email"}).encode())


def test_parse_rejects_non_object_payload():
    with pytest.raises(ValueError):
        SendRequest.parse(b'"just a string"')


def test_send_returns_success_on_osascript_zero_exit():
    handler = OutboundHandler()
    req = SendRequest(to="+16125550123", service="iMessage", text="hi", request_id="r1")

    fake = type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
    with patch("subprocess.run", return_value=fake) as run:
        result = handler.send(req)

    run.assert_called_once()
    args = run.call_args.args[0]
    assert args[0].endswith("osascript") or args[0] == "osascript"
    # arguments after the script are: service, to, text
    assert "iMessage" in args
    assert "+16125550123" in args
    assert "hi" in args
    assert result["success"] is True
    assert result["error"] is None
    assert result["request_id"] == "r1"


def test_send_returns_failure_on_nonzero_exit():
    handler = OutboundHandler()
    req = SendRequest(to="+16125550123", service="iMessage", text="hi")
    fake = type("R", (), {"returncode": 1, "stdout": "", "stderr": "Service not found"})()
    with patch("subprocess.run", return_value=fake):
        result = handler.send(req)
    assert result["success"] is False
    assert "Service not found" in (result["error"] or "")


def test_send_returns_failure_on_timeout():
    import subprocess
    handler = OutboundHandler()
    req = SendRequest(to="+16125550123", service="iMessage", text="hi")
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=15)):
        result = handler.send(req)
    assert result["success"] is False
    assert "timed out" in (result["error"] or "").lower()


def test_send_returns_failure_on_missing_osascript():
    handler = OutboundHandler()
    req = SendRequest(to="+16125550123", service="iMessage", text="hi")
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        result = handler.send(req)
    assert result["success"] is False
    assert "osascript not found" in (result["error"] or "")
