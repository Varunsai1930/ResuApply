# ResuApply

A local job application copilot web app. It turns your verified profile and a pasted job description into an evidence checklist, a tailored resume, reviewed answers and an application record, **without inventing anything**.

> **Status:** Milestones 1 (Foundation) and 2 (Assessment) are built: profile, jobs, tracker, requirements with a deterministic checklist, evidence and overrides, and optional AI help through OpenRouter. Tailoring and answers come in later milestones; see [PLAN.md](PLAN.md). The same workflow is already available as an agent skill for Claude Code and Codex: [ResuSkill](https://github.com/Varunsai1930/ResuSkill).

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
| `OPENROUTER_API_KEY` | *(empty)* | Your [OpenRouter](https://openrouter.ai) key. Without it the AI buttons are hidden and everything else works |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3.5-lightning:free` | Any OpenRouter model ID that supports tool calling |
| `OPENROUTER_MODEL_TRUST` | `free` | `free`: you approve what is sent before the first AI request that includes your career history. `trusted`: that review is optional |

The bind address is always `127.0.0.1` and cannot be configured. The app also rejects requests whose `Host` isn't `127.0.0.1` or `localhost`, and form posts coming from other websites.

Tables are created on startup, and columns added by a later milestone are added to an existing database automatically (nothing is renamed or dropped). To start over, stop the app and delete `data/resuapply.db`. The database, `.env`, uploads and generated files are git-ignored.

### AI model

The model is a setting, and the app never switches it on its own. Free models can change availability, limits or tool-calling support at any time; if the configured one stops working, the app reports the provider's error, keeps your work and the manual workflow keeps going. Changing `OPENROUTER_MODEL` is always your decision.

- Each request offers one tool that only returns data in a fixed schema. The result is validated locally; malformed or invalid output gets one corrective retry, then an error is shown. Requests time out after 90 seconds.
- Validated results are stored and reused while the inputs, model and prompt revision are unchanged. The app records the model, prompt revision, input revisions, time and token usage. Logs never contain your key, prompts, answers or any profile or job text.
- **Extracting requirements** sends only the job's title, company, location and description.
- **Suggesting evidence** sends a reduced copy of your career history: never your contact details, links, work authorization, preferences, availability, GPA, job locations or skills you said you lack. With a `free` model you see an editable preview at **Profile → What the AI model may see** and approve it first; you can leave out or reword any item. The approval is reused until a profile change alters what would be sent. Redaction is best effort.

To check a key and model with fictional data (a throwaway database; your `data/` is untouched):

```bash
.venv/bin/python -m scripts.smoke_openrouter
```

To try the AI screens without a key, run the app against a simple local stand-in for the model (nothing leaves your machine):

```bash
RESUAPPLY_DATA_DIR=data/demo .venv/bin/python -m scripts.fake_ai_server
```

## What's included

- **Profile:** a guided form for contact details, links, summary, education, experience, projects (with bullets and technologies), skills, skills you've confirmed you lack, certifications, preferences, availability and per-country work authorization (yes / no / unknown). Every save goes through a **Review changes** step that lists each added, changed or removed fact. Entries, bullets and certifications get stable IDs (`exp-1`, `exp-1-b2`, `cert-1`) that survive edits, and deleted IDs are never reused. The revision increases only when something really changed. The validation and ID rules are ported from ResuSkill.
- **Jobs:** manual entry with title, company and pasted description (required), plus location and URL (optional). The description is stored exactly as pasted and is only ever shown as text. Changing the title, company, location or description increases the job revision.
- **Tracker:** every job with its review state (Draft until package review arrives) and tracking status (Saved, Applied, Assessment, Interview, Rejected, Offer, Withdrawn), a dated status history and notes. Only you change the status; approval never sets Applied.
- **Job Workspace:** the job's details and description, tracking and notes, the requirements checklist, and marked placeholders for Resume, Questions and Review.
- **Requirements:** enter them yourself or have the AI model propose them; either way you review and correct each one before saving. Every requirement quotes words from the description, and if any excerpt can't be found there, nothing is saved. Each can have a comparable criterion (skills, degree, graduation window, location/work mode, work authorization, start date, years of experience). Requirement IDs (`r1`, `r2` …) stay stable across edits and are never reused.
- **Checklist:** Python compares each requirement with your profile and shows **Met**, **Unmet** or **Unknown** with the reason, grouped as sources, gaps and unknowns. A skill that isn't in your profile is Unknown unless you confirmed you lack it; anything that can't be compared stays Unknown until you link evidence. Gaps stay visible but never block an application.
- **Evidence and overrides:** link profile bullets or entries as evidence yourself, or ask the AI model for suggestions and accept or reject each one. Suggestions change nothing until you accept them. You can override any status, and the override is always shown with your reason and the calculated status.

## Project layout

```text
app/
  main.py         FastAPI app factory (startup creates tables)
  config.py       Settings from the environment / .env
  db.py           SQLAlchemy engine, sessions, Pydantic-validated JSON columns
  models.py       candidates, jobs, applications, answer_bank
  schemas/        Pydantic shapes for the profile and tracking JSON
  ai/             OpenRouter client, prompts, requirement extraction and evidence suggestions
  services/       Rules ported from ResuSkill (profile, requirements, checklist), outbound context,
                  form parsing, jobs, tracking
  routes/         Profile and jobs pages
  templates/      Jinja2 pages
  static/         CSS and a small amount of JavaScript
scripts/          Live OpenRouter smoke test and a fake-AI dev server
tests/            pytest: rules, mocked AI, HTTP integration and restart persistence
```

## Releases

| Release | Scope |
|---|---|
| **V1** | Profile, job entry, requirement checklist, resume tailoring, answers, approval, print-to-PDF, tracking |
| **V2** | PDF/DOCX profile import, Greenhouse job import, limited Greenhouse autofill (you still click Submit) |

## Stack

Python 3.12 · FastAPI · Jinja2 · SQLAlchemy · SQLite · Pydantic · HTTPX → OpenRouter (model configurable). Playwright only in V2.

## License

[MIT](LICENSE)
