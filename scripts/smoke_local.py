"""Offline acceptance of the real local CLI, also runnable on Windows.

Run ``python -m scripts.smoke_local`` from the repository. All application data
is synthetic and temporary; an existing .env or data folder is never changed.
``--print-pdf FILE`` additionally checks Chromium printing (requires Playwright
and ``python -m playwright install chromium``). Browser dependencies are CI-only.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing, contextmanager
from html import unescape
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
NAME = "Jordan Smoke"
BULLET = "Built a Python API serving internal reports."
LINES = [NAME, "jordan.smoke@example.com", "SUMMARY", "Python engineer building local tools.",
         "EXPERIENCE", "Software Engineer | Example Labs | Jan 2024 - Dec 2024", f"- {BULLET}",
         "SKILLS", "Python, SQL"]
PROFILE_FORM = {
    "contact-name": NAME, "contact-email": "jordan.smoke@example.com", "contact-location": "Remote",
    "summary": "Python engineer building local tools.", "experience-0-organization": "Example Labs",
    "experience-0-title": "Software Engineer", "experience-0-start": "2024-01", "experience-0-end": "2024-12",
    "experience-0-bullets-0-text": BULLET, "skills": "Python, SQL",
}


def require(condition, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def form_value(page: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}"[^>]*value="([^"]*)"', page)
    require(match is not None, f"The form did not contain {name}.")
    return unescape(match.group(1))


def resume_files() -> list[tuple[str, bytes, str]]:
    """A synthetic text PDF and Word file built without any external fixture."""
    from docx import Document

    document = Document()
    for line in LINES:
        document.add_paragraph(line)
    word = io.BytesIO()
    document.save(word)
    # A small text-only PDF with a normal Helvetica font and a complete cross-reference table.
    body = ("BT /F1 11 Tf 14 TL 72 760 Td " + " ".join(f"({line}) Tj T*" for line in LINES) + " ET").encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf, offsets = io.BytesIO(), []
    pdf.write(b"%PDF-1.4\n")
    for number, content in enumerate(objects, 1):
        offsets.append(pdf.tell())
        pdf.write(f"{number} 0 obj\n".encode() + content + b"\nendobj\n")
    xref = pdf.tell()
    pdf.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        pdf.write(f"{offset:010d} 00000 n \n".encode())
    pdf.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return [("resume.pdf", pdf.getvalue(), "application/pdf"),
            ("resume.docx", word.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")]


def port_number() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def running_server(env: dict[str, str], base_url: str, log_path: Path):
    """Start the supported CLI and reap it on success, failure or interruption."""
    process = None
    with log_path.open("wb") as log:
        try:
            process = subprocess.Popen([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                                       stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 30
            # The CLI's earlier "ResuApply running at" is only a preflight message.
            # Uvicorn emits this marker after its own socket has actually bound.
            bound_marker = f"Uvicorn running on {base_url} ("
            with httpx.Client(trust_env=False, timeout=1) as health:
                while time.monotonic() < deadline:
                    require(process.poll() is None,
                            "The local server exited during startup; inspect " + str(log_path))
                    if bound_marker in log_path.read_text(encoding="utf-8", errors="replace"):
                        try:
                            if health.get(base_url + "/welcome").status_code == 200 and process.poll() is None:
                                break
                        except httpx.HTTPError:
                            pass
                    time.sleep(0.05)
                else:
                    raise RuntimeError("The local server did not become ready within 30 seconds.")
            yield process
        finally:
            if process is not None:
                if process.poll() is None:
                    # On Windows this uses TerminateProcess; on POSIX it sends SIGTERM.
                    # Every write is already committed before the persistence restart.
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def stored_state(database: Path):
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        return (connection.execute("SELECT profile, revision, id_counters FROM candidates").fetchall(),
                connection.execute("SELECT title, company, revision FROM jobs").fetchall(),
                connection.execute("SELECT accepted_package FROM applications").fetchall())


def browser_pdf(base_url: str, job_id: int, target: Path) -> None:
    from pypdf import PdfReader

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("PDF printing needs Playwright: install playwright and run "
                           "python -m playwright install chromium, then retry --print-pdf.") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            context = browser.new_context()
            # The browser may only request this temporary local application.
            context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(base_url + "/")
                          else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            for path in ("/profile", f"/jobs/{job_id}", f"/jobs/{job_id}/resume/print"):
                response = page.goto(base_url + path, wait_until="networkidle")
                require(response is not None and response.status == 200, f"Browser could not open {path}.")
            page.evaluate("document.fonts.ready")
            page.screenshot(path=str(target.with_suffix(".png")), full_page=True)
            page.pdf(path=str(target), print_background=True, prefer_css_page_size=True)
            require(not errors, "Browser errors: " + "; ".join(errors))
        finally:
            browser.close()
    text = "\n".join(page.extract_text() or "" for page in PdfReader(target).pages)
    require(NAME in text and BULLET in text, "The printed PDF lost expected resume text.")


def run(output_dir: Path | None = None, print_pdf: Path | None = None) -> dict:
    report = {"platform": platform.system(), "python": platform.python_version(), "checks": [],
              "browser_pdf": "not requested" if print_pdf is None else "pending"}
    checks = report["checks"]
    with tempfile.TemporaryDirectory(prefix="resuapply acceptance ") as temporary:
        work = Path(temporary)
        data = work / "data with spaces Zoë"
        results = output_dir.resolve() if output_dir is not None else work
        results.mkdir(parents=True, exist_ok=True)
        port = port_number()
        base_url = f"http://127.0.0.1:{port}"
        env = os.environ | {"RESUAPPLY_DATA_DIR": str(data), "RESUAPPLY_PORT": str(port),
                            "OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": "smoke/local:free",
                            "OPENROUTER_MODEL_TRUST": "free", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"}
        database = data / "resuapply.db"
        with httpx.Client(base_url=base_url, trust_env=False, timeout=10, follow_redirects=False) as client:
            with running_server(env, base_url, results / "first-start.log"):
                require(client.get("/").headers.get("location") == "/welcome", "A fresh install did not show welcome.")
                require(client.get("/profile").status_code == 200, "The profile page did not render.")
                checks.append("real CLI startup and HTTP health")
                # Exercise the platform's occupied-port branch while the first CLI owns it.
                occupied = subprocess.run([sys.executable, "-m", "app"], cwd=ROOT, env=env,
                                          capture_output=True, text=True, encoding="utf-8", timeout=15)
                require(occupied.returncode != 0 and "already in use" in occupied.stdout + occupied.stderr,
                        "A second CLI did not reject the occupied port.")
                checks.append("occupied port rejected")
                for filename, content, content_type in resume_files():
                    imported = client.post("/profile/import", files={"file": (filename, content, content_type)})
                    require(imported.status_code == 200 and form_value(imported.text, "contact-name") == NAME,
                            f"The {filename} import did not fill the profile editor.")
                require(stored_state(database)[0] == [], "Import saved a profile before review.")
                checks.append("PDF and DOCX import, with no save before review")
                review = client.post("/profile/review", data=PROFILE_FORM)
                require(review.status_code == 200, "The synthetic profile could not be reviewed.")
                saved = client.post("/profile/save", data={"payload": form_value(review.text, "payload"),
                                                          "base_revision": form_value(review.text, "base_revision")})
                require(saved.status_code == 303, "The reviewed profile did not save.")
                created = client.post("/jobs", data={"title": "Local Smoke Engineer", "company": "Example Company",
                                                    "description": "Build Python services and test local tools."})
                require(created.status_code == 303, "The synthetic job did not save.")
                match = re.match(r"/jobs/(\d+)", created.headers.get("location", ""))
                require(match is not None, "The new job did not have a workspace URL.")
                job_id = int(match.group(1))
                proposed = client.post(f"/jobs/{job_id}/resume/profile")
                require(proposed.status_code == 303, "A resume could not be prepared without AI.")
                proposal = client.get(f"/jobs/{job_id}/resume/review")
                require(proposal.status_code == 200, "The resume proposal could not be reviewed.")
                accepted = client.post(f"/jobs/{job_id}/resume/accept",
                                       data={"proposal_token": form_value(proposal.text, "proposal_token")})
                require(accepted.status_code == 303, "The reviewed resume did not accept.")
                printed = client.get(f"/jobs/{job_id}/resume/print")
                require(printed.status_code == 200 and NAME in printed.text and BULLET in printed.text,
                        "The accepted resume did not render as print HTML.")
                checks.append("reviewed profile save, job save and accepted print HTML")
                before = stored_state(database)
            require("AI off (no OPENROUTER_API_KEY)" in (results / "first-start.log").read_text(encoding="utf-8"),
                    "The smoke server was not running with AI disabled.")
            checks.append("AI disabled despite inherited environment settings")
            with socket.socket() as probe:
                probe.settimeout(1)
                require(probe.connect_ex(("127.0.0.1", port)) != 0, "The stopped server still owns its port.")
            checks.append("server shutdown releases its port")
            with running_server(env, base_url, results / "restart.log"):
                require(NAME in client.get("/profile").text, "The saved profile did not survive restart.")
                require("Example Company" in client.get(f"/jobs/{job_id}").text, "The job did not survive restart.")
                restarted_print = client.get(f"/jobs/{job_id}/resume/print")
                require(restarted_print.status_code == 200 and BULLET in restarted_print.text,
                        "The accepted resume did not survive restart.")
                require(stored_state(database) == before, "Restart changed the saved profile, job or accepted resume.")
                checks.append("restart persistence in a path with spaces and Unicode")
                if print_pdf is not None:
                    browser_pdf(base_url, job_id, print_pdf.resolve())
                    report["browser_pdf"] = "passed"
                    checks.append("Chromium HTML rendering and PDF text")
    report["temporary_data_removed"] = not work.exists()
    report["status"] = "passed"
    if output_dir is not None:
        (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, help="Keep synthetic logs and report here; app data is always deleted.")
    parser.add_argument("--print-pdf", type=Path, help="Also render the accepted resume to this PDF with Playwright Chromium.")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.output_dir, args.print_pdf), indent=2))
    except Exception as exc:
        print(f"Local acceptance failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
