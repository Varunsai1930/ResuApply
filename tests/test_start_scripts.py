"""One-command start: the package check behind start.sh / start.bat, the launchers themselves and --open."""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app import __main__ as entry
from scripts import ensure_packages

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def stamp(tmp_path, monkeypatch):
    path = tmp_path / ".resuapply-requirements"
    monkeypatch.setattr(ensure_packages, "STAMP", path)
    return path


def test_packages_install_once_and_again_only_when_requirements_change(stamp, tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi==1.0\n")
    monkeypatch.setattr(ensure_packages, "REQUIREMENTS", requirements)
    runs = []
    monkeypatch.setattr(ensure_packages.subprocess, "run", lambda command, check: runs.append(command)
                        or subprocess.CompletedProcess(command, 0))
    assert ensure_packages.main() == 0 and len(runs) == 1  # first start installs
    assert stamp.read_text().strip() == ensure_packages.digest(requirements)
    assert ensure_packages.main() == 0 and len(runs) == 1  # unchanged: nothing to do
    requirements.write_text("fastapi==2.0\n")
    assert ensure_packages.main() == 0 and len(runs) == 2  # a pull changed them


def test_a_failed_install_keeps_asking_next_time(stamp, monkeypatch, capsys):
    monkeypatch.setattr(ensure_packages.subprocess, "run", lambda command, check: subprocess.CompletedProcess(command, 1))
    assert ensure_packages.main() == 1 and not stamp.exists()
    assert "Installing the packages failed" in capsys.readouterr().out


def test_without_pip_it_uses_uv_or_says_what_to_do(monkeypatch):
    monkeypatch.setattr(ensure_packages.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(ensure_packages.shutil, "which", lambda name: "/usr/bin/uv")
    command = ensure_packages.install_command()
    assert command[:3] == ["/usr/bin/uv", "pip", "install"] and sys.executable in command
    monkeypatch.setattr(ensure_packages.shutil, "which", lambda name: None)
    assert ensure_packages.install_command() is None


def test_the_launchers_are_valid_and_start_the_app_with_open():
    sh = (ROOT / "start.sh").read_text()
    assert subprocess.run(["sh", "-n", str(ROOT / "start.sh")]).returncode == 0
    assert "python -m scripts.ensure_packages" in sh and "python -m app --open" in sh and "chmod 600 .env" in sh
    bat = (ROOT / "start.bat").read_bytes()
    assert b"\r\n" in bat and b"\n" not in bat.replace(b"\r\n", b"")  # Windows line endings, for goto labels
    assert b"-m scripts.ensure_packages" in bat and b"-m app --open" in bat and b"py -3.12" in bat


def test_open_waits_for_the_server_then_opens_the_browser(monkeypatch):
    opened = []
    monkeypatch.setattr(entry.webbrowser, "open", opened.append)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        thread = entry.open_when_ready(f"http://127.0.0.1:{port}", port, timeout=5)
        threading.Event().wait(0.6)
        assert opened == []  # not listening yet
        server.listen()
        thread.join(5)
    assert opened == [f"http://127.0.0.1:{port}"]
