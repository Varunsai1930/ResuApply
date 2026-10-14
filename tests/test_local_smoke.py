"""Acceptance of the real launcher, isolated from the developer's files and AI key."""

import builtins
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pytest

from scripts.smoke_local import ROOT, running_server
from scripts import smoke_local


def test_real_local_smoke_uses_temporary_data_and_disables_ai(tmp_path):
    output = tmp_path / "synthetic smoke results"
    env = os.environ | {"OPENROUTER_API_KEY": "sk-test-must-never-be-used"}
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.smoke_local", "--output-dir", str(output)],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    report = json.loads(completed.stdout)
    assert report["status"] == "passed" and report["temporary_data_removed"]
    assert report["browser_pdf"] == "not requested"
    assert len(report["checks"]) == 7
    assert json.loads((output / "report.json").read_text(encoding="utf-8")) == report
    for filename in ("first-start.log", "restart.log"):
        log = (output / filename).read_text(encoding="utf-8")
        assert "AI off (no OPENROUTER_API_KEY)" in log and "sk-test-must-never-be-used" not in log
    assert not list(output.rglob("*.db"))


def test_a_healthy_unrelated_server_cannot_satisfy_launcher_readiness(tmp_path):
    requests = []

    class UnrelatedApp(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            body = b"A healthy unrelated test app"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), UnrelatedApp)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    env = os.environ | {"RESUAPPLY_DATA_DIR": str(tmp_path / "temporary app data"),
                        "RESUAPPLY_PORT": str(server.server_port), "OPENROUTER_API_KEY": "",
                        "OPENROUTER_MODEL": "smoke/local:free", "OPENROUTER_MODEL_TRUST": "free",
                        "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
    log = tmp_path / "failed-start.log"
    try:
        with httpx.Client(trust_env=False, timeout=1) as client:
            assert client.get(base_url + "/welcome").status_code == 200
        requests.clear()
        with pytest.raises(RuntimeError, match="exited during startup"):
            with running_server(env, base_url, log):
                pytest.fail("The harness accepted somebody else's welcome page.")
        assert "already in use" in log.read_text(encoding="utf-8")
        assert requests == []  # neither a health request nor an import/save reached the unrelated app
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


def test_missing_browser_dependency_returns_a_plain_error_and_cleans_up(tmp_path, monkeypatch, capsys):
    original_import = builtins.__import__

    def without_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("Playwright deliberately unavailable in this acceptance test")
        return original_import(name, *args, **kwargs)

    output = tmp_path / "failed browser results"
    pdf = output / "accepted.pdf"
    monkeypatch.setattr(builtins, "__import__", without_playwright)
    monkeypatch.setattr(sys, "argv", ["smoke_local", "--output-dir", str(output), "--print-pdf", str(pdf)])
    assert smoke_local.main() == 1
    captured = capsys.readouterr()
    assert "PDF printing needs Playwright" in captured.err
    assert "python -m playwright install chromium" in captured.err
    assert "Traceback" not in captured.err and not pdf.exists()
    log = (output / "first-start.log").read_text(encoding="utf-8")
    database = re.search(r"ResuApply running at .+ \(data: (.+)\)", log)
    assert database is not None
    assert not Path(database.group(1)).parent.exists()  # failure also removes the temporary app data
