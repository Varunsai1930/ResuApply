# ResuApply: session handoff

Updated 2026-10-11. Repository: [Varunsai1930/ResuApply](https://github.com/Varunsai1930/ResuApply), branch `main`.

## Where things stand

- `main` on GitHub ends at `dc59396` (the Greenhouse import merge). Every feature branch has been merged and deleted; only `main` is left.
- The full test suite passes: **1,352 tests** (`.venv/bin/python -m pytest`), up from 937 at the last handoff.
- **The MVP plan is complete.** All nine steps below are built, reviewed and on `main`, plus a light and dark mode toggle.
- Two V2 features are also built: **resume import** (PDF/DOCX, read locally) and **Greenhouse job import**. Graduation year and date questions are now answered from the profile.
- The app is ready for friends and teammates to run locally. AI features have still only been tested with a stand-in provider.
- **Next:** see "What's left" below. Live AI testing comes first.

## Working agreements

- **One commit per change.** Each fix or feature step gets its own commit as soon as it passes tests. (Also saved as a Claude memory.)
- **Push only when asked.** Commits stay local until the push is confirmed.
- Every fix gets a regression test, checked to fail without the fix.
- Commit messages explain the problem and the fix, and end with the `Co-Authored-By` line.
- **Review before merging.** Branches from other sessions are reviewed (and their tests run) before they go into `main`; findings go back to the session that wrote them.
- Run `git fetch` and `git status` before starting, and re-run the tests after pulling.

## Working with cloud sessions

Claude cloud sessions can't push to this repo yet: their GitHub connection isn't linked to the `Varunsai1930` account, so pushes fail with a 403. Until that's fixed:

1. The cloud session attaches a git bundle of its branch, built on the branch's current GitHub head.
2. On the Mac: `git bundle verify`, then `git fetch <bundle> <branch>:<branch>`, run the tests, review, then push the branch.

To let cloud sessions push directly, reconnect GitHub as `Varunsai1930` at https://claude.ai/customize/connectors and install the Claude GitHub App on the repo. Commits made in cloud sessions are authored as `Claude <noreply@anthropic.com>`.

## What happened, in order

### 1. Reviews and testing (before the last handoff)
Five code reviews and one black-box test round: every finding fixed, with regression tests. See the git history from `b2e9fcc` to `6f9c0bb`.

### 2. MVP design
A clickable Design canvas: [ResuApply MVP](https://claude.ai/artifact/5a5nSzeR8bhJA7Q2uX7st8) (private; share it from its Share menu). Ten screens, built on the app's own tokens from `app/static/style.css`.

### 3. MVP implementation (`91ff305`..`eb4fe13`)
1. **Four shared status meanings** (`app/services/status.py`): Done, Needs you, Blocked, Info, each with its icon, and a shared header (`_header.html`). Blocked only means something stops you continuing (package blockers, a step waiting on an earlier one); requirement gaps (Unmet) and Unknowns are Info, since gaps never block an application.
2. **Job workspace as five guided steps** (`/jobs/{id}?step=requirements|resume|questions|review|track`, `app/services/workspace.py`) with one Next action.
3. **Jobs board** (`app/services/board.py`): columns by tracking status, each card with its next action, a summary strip and search.
4. **Follow-up reminders**: the `applications.follow_up_after` column, "Remind me after…", and the board banner.
5. **Visible trust in resume review**: cited profile lines highlighted, and a word-level diff.
6. **Profile improvements**: "What your applications still ask for" (`profile_gaps.py`), citation counts per bullet (`citations.py`), editing one section at a time.
7. **Answer bank and Add a job polish**: search, sort and usage counts; title, company and location filled in from a pasted posting.
8. **Onboarding**: a welcome page and **Load sample data** (`sample.py`), removable.
9. Light and dark mode toggle in the header.

### 4. Resume import (V2), `a1dba71`..`0d619f3`
Choose a PDF or Word resume on the Profile or Welcome page. It's read on this computer only (`app/services/resume_import.py` for the text, `resume_draft.py` for the draft profile) into the profile form, then goes through the usual review step. With a saved profile, it only adds what the profile lacks and never changes a saved fact. Work authorization is never read from a resume. A cloud session hardened it against a corpus of synthetic layouts (`tests/test_resume_corpus.py`): two-column PDFs, Word tables, text boxes, headers and footers, many bullet and date styles. The README lists what it reads and what it can't.

The review before merging found and fixed: quadratic date matching, skills you said you lack being re-added, duplicate certifications, a hang on a Word style based on itself, and a crash on deeply nested brackets.

### 5. Graduation answers, `9d442dc`..`2f27a0a`
"Graduation year" and "graduation date" questions are factual now, filled from the education end date in the format the label asks for (for example `(MM/YYYY)`). They're left to the user when the date isn't precise enough, a degree is still in progress, or several degrees disagree and the question doesn't say which. Saved questions are upgraded at startup.

### 6. Greenhouse job import (V2), `8f71fc8`..`21b059b`
On **Add a job**, a `boards.greenhouse.io` or `job-boards.greenhouse.io` link can be imported (`app/services/greenhouse.py`). The app fetches the posting and its application questions from `boards-api.greenhouse.io` only, refuses redirects, and gives up after 2 MB or the time limit. The questions are listed for review and added when the job is saved; demographic and self-identification questions are always Sensitive. Each question keeps its form field type in the new `jobs.posting_import` column. This is the app's only outbound request besides the AI provider, and it only happens when the user chooses Import.

Known nit: the time limit is effectively about 7 seconds, not 10, because a body read only starts while a full 3-second wait still fits before the deadline. Greenhouse normally answers in under a second.

## What's left

1. **Live AI test.** Run every AI screen with a real OpenRouter key and model, using synthetic, non-identifying content. Do this locally: the key shouldn't go into a cloud session.
2. **Real resumes.** Try the import on a few real PDF and Word resumes (two-column ones especially). Keep them on the Mac: they hold real people's details.
3. **Windows.** Documented and fixed in code, but not yet run on a Windows machine.
4. **Greenhouse autofill (V2, PLAN.md §4).** A visible Playwright browser, launched only by the user, fills verified fields from an approved package and never clicks Submit. It runs on the user's machine, so build and test it locally.
5. **Share the MVP canvas** if teammates should see it.

## How the app works now (for the next person)

- **Startup** (`app/main.py` lifespan, in order):
  1. `upgrade_stored_questions` (now also turns saved graduation questions factual)
  2. `rewrite_approvals_with_stored_rules`
  3. `upgrade_stored_approvals`

  `python -m app` first runs `load_settings`, `check_storage` and `check_port`.
- **Middleware, outermost first:**
  1. `NoFramingMiddleware`
  2. `TrustedHostMiddleware`
  3. `SameOriginMiddleware`
  4. `HeadAsGetMiddleware`
  5. `ControlCharacterMiddleware` (URL-encoded forms only; the resume upload is multipart)
- **Approval rules:** to make approval checks stricter, raise `APPROVAL_RULES` in `app/services/package.py`. An approval is trusted only when `approval_rules_for` equals its `approval_key`.
- **Compatibility:** keep the database readable by older versions. New data goes into new nullable columns (`follow_up_after`, `posting_import`) or is dropped on read through `PydanticJSON(read_upgrade=...)`, never added to the stored approval or snapshot data.
- **Outbound requests:** only OpenRouter (approved, reduced context) and Greenhouse's job board (on Import). Tests never use the network: `create_app(ai_transport=..., greenhouse_transport=...)`.
- **Key files:**
  - `app/services/questions.py` (classifier, factual values, graduation rules)
  - `app/services/answers.py` (questions, answers, bank, `measure`)
  - `app/services/package.py` (approval, review state, snapshots)
  - `app/services/status.py`, `workspace.py`, `board.py`
  - `app/services/resume_import.py`, `resume_draft.py`
  - `app/services/greenhouse.py`, `app/schemas/posting_import.py`
  - `app/services/profile.py`, `profile_form.py`, `profile_gaps.py`
  - `app/routes/*`, `app/templates/*`, `app/static/style.css`
- **Regression tests:**
  - `tests/test_qa_regressions.py`
  - `tests/test_answer_review_regressions.py`
  - `tests/test_review_upgrade.py`
  - `tests/test_resume_corpus.py`, `tests/test_resume_draft.py`, `tests/test_resume_import.py`
  - `tests/test_greenhouse.py`, `tests/test_greenhouse_import_routes.py`
  - `tests/test_graduation_answers.py`

## Running it

```bash
git clone https://github.com/Varunsai1930/ResuApply.git
cd ResuApply
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python -m app
```

Then open http://127.0.0.1:8000. After pulling, re-run the install: the resume import added `pypdf`, `python-docx` and `cryptography`. For Windows, see the README. For the AI screens without a key: `RESUAPPLY_DATA_DIR=data/demo .venv/bin/python -m scripts.fake_ai_server`.

## Prompt to start the next session

> I'm continuing work on ResuApply (this repo). Read `handsoff.md` first: it has the current state, the working agreements (one commit per change, a regression test per change, review before merging, push only when I ask) and what's left. The MVP plan is done, and resume import, graduation answers and Greenhouse job import are on `main`.
>
> Next is the live AI test: help me set up an OpenRouter key in `.env`, then walk every AI screen (requirements, evidence suggestions, resume tailoring, open-answer drafts) with the sample data, and report what breaks. Before starting, run `git fetch` and `git status` and the test suite. Don't push until I ask.
