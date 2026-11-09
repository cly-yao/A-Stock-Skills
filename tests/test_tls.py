"""TLS verification stays on. A bad certificate is a failed fetch, not a data source."""

import inspect
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import requests

import astock_runtime
from skills_import import load

ROOT = Path(__file__).resolve().parents[1]
report = load("skills/05-reports/report/main.py", "report_tls_under_test")


def _assert_verify_enabled(verify):
    assert verify is not False
    assert verify is not None
    if isinstance(verify, str):
        assert Path(verify).is_file()
    else:
        assert verify is True


def test_default_verify_is_enabled():
    _assert_verify_enabled(astock_runtime.tls_verify())
    budget = astock_runtime.HttpBudget(max_calls=1, timeout=2)
    _assert_verify_enabled(budget.verify)
    _assert_verify_enabled(budget._session.verify)


def test_verify_cannot_be_turned_off():
    with pytest.raises(RuntimeError, match="不能关闭"):
        astock_runtime.HttpBudget(max_calls=1, timeout=1, verify=False)


def test_http_get_and_budget_pass_verify(monkeypatch):
    seen = []

    def spy_get(url, **kwargs):
        seen.append(kwargs.get("verify", "MISSING"))

        class Response:
            status_code = 200
            text = ""
            encoding = None

        return Response()

    def spy_request(self, method, url, **kwargs):
        seen.append(kwargs.get("verify", "MISSING"))
        raise requests.exceptions.SSLError("certificate verify failed: unit")

    monkeypatch.setattr(requests, "get", spy_get)
    monkeypatch.setattr(requests.Session, "request", spy_request)
    astock_runtime.http_get("https://example.test/quote", timeout=1)
    budget = astock_runtime.HttpBudget(max_calls=1, timeout=1)
    with pytest.raises(requests.exceptions.SSLError):
        budget.request("GET", "https://example.test/quote")
    assert seen
    for verify in seen:
        _assert_verify_enabled(verify)


def test_proxy_patch_does_not_clear_verify(monkeypatch):
    captured = {}

    def spy(url, **kwargs):
        captured["verify"] = kwargs.get("verify", "MISSING")

        class Response:
            status_code = 200
            text = ""
            encoding = None

            def raise_for_status(self):
                return None

            def json(self):
                return {}

        return Response()

    monkeypatch.setattr(requests, "get", spy)
    module = load("skills/01-infra/astock-data-source/main.py", "data_source_tls_under_test")
    source = inspect.getsource(module._patch_akshare_proxy)
    assert "verify=" not in source
    assert '["verify"]' not in source
    requests.get("https://example.test/quote", timeout=1, verify=astock_runtime.tls_verify())
    _assert_verify_enabled(captured["verify"])


def test_production_sources_do_not_disable_tls():
    hits = []
    needles = ("verify=False", "verify = False", '["verify"] = False', "['verify'] = False")
    for path in ROOT.rglob("*.py"):
        if any(part in {".git", "tests", "__pycache__"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        for needle in needles:
            if needle in text:
                hits.append(f"{path}: {needle}")
    assert hits == []


def _self_signed_server(directory: Path):
    cert = directory / "cert.pem"
    key = directory / "key.pem"
    subprocess.check_call(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key), "-out", str(cert),
            "-days", "1", "-nodes", "-subj", "/CN=localhost",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"price": 999999, "name": "not-a-quote"}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_bad_certificate_fails_the_report_without_using_the_body(tmp_path):
    server = _self_signed_server(tmp_path)
    port = server.server_address[1]
    forged = f"https://127.0.0.1:{port}/quote"

    class BadCertBudget(astock_runtime.HttpBudget):
        def request(self, method, url, params=None, headers=None, encoding=None):
            return super().request(method, forged, params=None, headers=headers, encoding=encoding)

    try:
        direct = astock_runtime.HttpBudget(max_calls=1, timeout=5)
        with pytest.raises(requests.exceptions.SSLError):
            direct.request("GET", forged)
        built = report.build_daily_report(
            transport=BadCertBudget(max_calls=4, timeout=5),
            provenance="synthetic",
        )
    finally:
        server.shutdown()

    assert built["provenance"] == "synthetic"
    assert built["sections"]
    assert all(section["status"] == "failed" for section in built["sections"])
    for section in built["sections"]:
        assert section["rows"] == []
        assert "SSLError" in (section.get("error") or "")
    text = report.render_markdown(built)
    assert "999999" not in text
    assert "not-a-quote" not in text
    assert "0.00" not in text
