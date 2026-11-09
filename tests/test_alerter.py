import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from skills_import import load

alerter = load("skills/06-tools/alerter/main.py", "alerter_under_test")


def test_feishu_payload_is_valid_json(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

    def fake_post(url, json=None, timeout=None, **kwargs):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    ok = alerter.send_feishu("https://example.test/hook", "hello")
    assert ok is True
    assert captured["url"] == "https://example.test/hook"
    assert captured["timeout"] == 10
    card = captured["json"]["card"]
    assert set(card) == {"header", "elements"}
    assert card["header"]["title"]["content"] == "A股告警"
    assert card["elements"] == [{"tag": "markdown", "content": "hello"}]
    json.dumps(captured["json"])
