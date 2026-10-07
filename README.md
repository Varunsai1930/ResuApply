# ResuApply

A local job application copilot web app. It turns your verified profile and a pasted job description into an evidence checklist, a tailored resume, reviewed answers and an application record, **without inventing anything**.

> **Status:** Milestone 1 (Foundation) is built: profile, manual job entry, tracker and Job Workspace. Requirement checks, tailoring and answers come in later milestones; see [PLAN.md](PLAN.md). The same workflow is already available as an agent skill for Claude Code and Codex: [ResuSkill](https://github.com/Varunsai1930/ResuSkill).

```text
Create profile → Add job → Review requirements
→ Tailor resume → Draft answers → Review package
→ Save resume as PDF → Submit manually → Track outcome
```

## Principles

- **Checklist, not scores.** Each requirement is Met, Unmet or Unknown, with its evidence. Missing information stays Unknown.
- **The model drafts; Python decides.** Every rewritten bullet cites profile sources. Drafts that add skills, numbers, dates or credentials are rejected before you see them.
- **You own the profile.** The model can never change it; every edit is yours.
- **You submit.** Approval never means applied, and the app never clicks an employer's Submit button.
- **Local first.** Runs on `127.0.0.1` with SQLite. Only the reduced career context you approve is sent to the configured model.

## Run it

Requires Python 3.12. These are the commands used to verify Milestone 1 on macOS.

```bash
git clone https://github.com/Varunsai1930/ResuApply.git
cd ResuApply
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python -m app
```

Then open <http://127.0.0.1:8000>. Stop the server with Ctrl+C.

If you use [uv](https://docs.astral.sh/uv/) and don't have Python 3.12 installed, create the environment with `uv venv --python 3.12 .venv` and install with `uv pip install --python .venv/bin/python -r requirements-dev.txt`. Use `requirements.txt` instead of `requirements-dev.txt` if you don't need the tests.

Run the tests:

```bash
.venv/bin/python -m pytest
```

### Configuration

Every setting is optional and is read from the environment or `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `RESUAPPLY_PORT` | `8000` | Port to listen on |
| `RESUAPPLY_DATA_DIR` | `data` | Folder for the SQLite database (`resuapply.db`); relative paths start at the project root |

The bind address is always `127.0.0.1` and cannot be configured. The app also rejects requests whose `Host` isn't `127.0.0.1` or `localhost`, and form posts coming from other websites.

Tables are created on startup; there are no migrations yet. To start over, stop the app and delete `data/resuapply.db`. The database, `.env`, uploads and generated files are git-ignored.

## What Milestone 1 includes

- **Profile:** a guided form for contact details, links, summary, education, experience, projects (with bullets and technologies), skills, skills you've confirmed you lack, certifications, preferences, availability and per-country work authorization (yes / no / unknown). Every save goes through a **Review changes** step that lists each added, changed or removed fact. Entries, bullets and certifications get stable IDs (`exp-1`, `exp-1-b2`, `cert-1`) that survive edits, and deleted IDs are never reused. The revision increases only when something really changed. The validation and ID rules are ported from ResuSkill.
- **Jobs:** manual entry with title, company and pasted description (required), plus location and URL (optional). The description is stored exactly as pasted and is only ever shown as text. Changing the title, company, location or description increases the job revision.
- **Tracker:** every job with its review state (Draft until package review arrives) and tracking status (Saved, Applied, Assessment, Interview, Rejected, Offer, Withdrawn), a dated status history and notes. Only you change the status; approval never sets Applied.
- **Job Workspace:** the job's details and description, tracking and notes, and marked placeholders for Requirements, Resume, Questions and Review.

## Project layout

```text
app/
  main.py         FastAPI app factory (startup creates tables)
  config.py       Settings from the environment / .env
  db.py           SQLAlchemy engine, sessions, Pydantic-validated JSON columns
  models.py       candidates, jobs, applications, answer_bank
  schemas/        Pydantic shapes for the profile and tracking JSON
  services/       Profile rules (ported from ResuSkill), form parsing, jobs, tracking
  routes/         Profile and jobs pages
  templates/      Jinja2 pages
  static/         CSS and a small amount of JavaScript
tests/            pytest: rules, HTTP integration and restart persistence
```

## Releases

| Release | Scope |
|---|---|
| **V1** | Profile, job entry, requirement checklist, resume tailoring, answers, approval, print-to-PDF, tracking |
| **V2** | PDF/DOCX profile import, Greenhouse job import, limited Greenhouse autofill (you still click Submit) |

## Stack

Python 3.12 · FastAPI · Jinja2 · SQLAlchemy · SQLite · Pydantic. Later milestones add HTTPX → OpenRouter (model configurable), and Playwright only in V2.

## License

[MIT](LICENSE)
