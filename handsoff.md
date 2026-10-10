# ResuApply: session handoff

Session date: 2026-10-10. Repository: [Varunsai1930/ResuApply](https://github.com/Varunsai1930/ResuApply), branch `main`.

## Where things stand

- `main` on GitHub ends at `6f9c0bb`, and the local copy matches it.
- The full test suite passes: **937 tests** (`.venv/bin/python -m pytest`). It was 777 at the start of the session.
- Five code reviews and one black-box test round were run. Every finding is fixed, committed and pushed (43 commits on `main` since `b2e9fcc`, including parallel work).
- The app is ready for friends and teammates to run locally. AI features have only been tested with a stand-in provider; live testing with a real OpenRouter key is still pending.
- An MVP redesign exists as a clickable Design canvas: [ResuApply MVP](https://claude.ai/artifact/5a5nSzeR8bhJA7Q2uX7st8). It's private: share it from the page's Share menu.
- **Next:** implement the MVP in the app, starting with plan steps 1 and 2 below.

## Working agreements

- **One commit per change.** Each fix or feature step gets its own commit as soon as it passes tests. (Also saved as a Claude memory.)
- **Push only when asked.** Commits stay local until the push is confirmed.
- Every fix gets a regression test, checked to fail without the fix.
- Commit messages explain the problem and the fix, and end with the `Co-Authored-By` line.
- Parallel work also happens on `main` (for example `8bdccbd`, `a46ec9f`, `2c368cf` to `098eb00`). Run `git fetch` and `git status` before starting, and re-run the tests after pulling.

## What happened, in order

### 1. Readiness check and first review (8 findings)
The question classifier missed common form labels. The requirements and job editors mishandled save conflicts. There were also three efficiency issues and a stale suggestion cache. All fixed, plus a Windows section in the README.

### 2. Reviews 2 to 4 (9 findings)
Startup upgrades of saved questions and approvals, an approval "rules version" kept in its own column (`approval_rules`, `approval_rules_for`), and keeping older app versions able to read the database. Answers a user typed are kept when a question's category changes; profile values and bank answers are never kept on a sensitive question.

### 3. Black-box test round (15 findings, fixed in `ce8638c`..`59d2747`)
- **Forms:** the editors read forms with higher limits (`app/routes/forms.py`), and requirements are capped at 200.
- **Security:** no framing allowed (`NoFramingMiddleware`), and IDs too large for the database are "not found" (`db.is_db_id`, `db.get_by_id`).
- **Validation:**
  - damaged profile review data is refused (`profile_form.is_form_shaped`)
  - years of experience must be finite, from 0 to 100
  - date ranges must not end before they start (`text.ends_before_start`)
  - control characters are stripped from every form value (`app/input_cleaning.py`)
  - status dates are `YYYY-MM-DD` from 1990 onwards
  - question limits are at most 10,000
  - profile links must be web addresses
  - the sharing return link is strict
- **Errors and startup:** friendly error pages (`app/main.py`), and plain startup checks for settings, data folder and port (`app/__main__.py`).
- **Counting:** answer length is counted the way employer forms count it (UTF-16 units; words split at dashes; `answers.measure`).
- **Small fixes:** phone layout fixes, and HEAD is served like GET.

### 4. Review 5 (4 findings, fixed in `bc2f86f`..`6f9c0bb`)
- Bank answers are never kept on a question that becomes sensitive.
- On Windows, the port probe uses exclusive binding.
- A bad query value is reported as a bad link.
- The editors ignore differences that are only legacy control characters (`text.without_controls`).

### 5. MVP design
No `/design` skill exists, so the Design canvas was used. Both design systems on the account are empty, so the design uses ResuApply's own tokens from `app/static/style.css`: paper `#f6f5f1`, ink `#17191c`, accent `#1f3a5f`, the met, unmet and unknown colours, Charter headings and system UI text.

The canvas has 10 linked screens:

1. Welcome and onboarding
2. Jobs board
3. Job workspace (step 2, Resume)
4. Review and approve (step 4)
5. Add a job by pasting once
6. Profile
7. Questions (step 3)
8. Submit and track (step 5)
9. Answer bank
10. Edit profile

Every screen uses the same four status meanings: **Done** (green check), **Needs you** (amber dot), **Blocked** (red lock) and **Info** (grey "i"). The nav uses equal-height links with a bottom border on the current page.

## Plan: implementing the MVP

Each step is self-contained and gets tests and its own commit.

1. **Shared building blocks (small).** A helper mapping existing statuses (Met/Unmet/Unknown, Draft/Approved/Stale, the question labels in `answers.LABELS`) onto Done, Needs you, Blocked and Info, with one icon set. A shared header partial in `base.html` with the fixed nav.
2. **Job workspace as guided steps (medium; start here).** Split `job_workspace.html` into five steps (`/jobs/{id}?step=requirements|resume|questions|review|track`), reusing `checklist.html`, `resume_section.html`, `questions_section.html`, `review_section.html` and the tracking section. Add `next_action(job, application, candidate)` built from `checklist.evaluate`, `resume_service.state`, `package.check` and tracking, giving each step's status and the one "Next" button. No new data.
3. **Jobs board (small to medium).** `/jobs` as columns by tracking status, each card showing its next action from step 2, plus a summary strip and search.
4. **Follow-up reminders (medium).** A nullable `applications.follow_up_after` column (added automatically by `_add_missing_columns`). A "Remind me after…" choice when recording Applied, "I followed up" and "Remind me in 3 days" actions, and the board banner.
5. **Visible trust in resume review (medium).** Highlight the profile lines each bullet cites and show a word-level diff (`difflib`, rendered as `<ins>` and `<del>`).
6. **Profile improvements (medium).** "What your applications still ask for": profile fields that saved jobs' factual questions need but the profile lacks, plus unknown authorization countries. How many resumes and answers cite each bullet. Section-at-a-time editing that keeps the existing review step.
7. **Answer bank and add-job polish (small).** Search, sort and a usage count (answers whose `bank_id` points to the entry). Title, company and location filled in from the first lines of a pasted posting, for the user to check.
8. **Onboarding (medium).** A welcome page when there's no profile, and **Load sample data** (the fictional profile and job, marked as sample and removable).
9. **Resume import from PDF/DOCX (large; V2 in PLAN.md).** New dependencies; parse into a draft profile that goes through the existing review step.

## How the app works now (for the next person)

- **Startup** (`app/main.py` lifespan, in order):
  1. `upgrade_stored_questions`
  2. `rewrite_approvals_with_stored_rules`
  3. `upgrade_stored_approvals`

  `python -m app` first runs `load_settings`, `check_storage` and `check_port`.
- **Middleware, outermost first:**
  1. `NoFramingMiddleware`
  2. `TrustedHostMiddleware`
  3. `SameOriginMiddleware`
  4. `HeadAsGetMiddleware`
  5. `ControlCharacterMiddleware`
- **Approval rules:** to make approval checks stricter, raise `APPROVAL_RULES` in `app/services/package.py`. An approval is trusted only when `approval_rules_for` equals its `approval_key`.
- **Compatibility:** keep the database readable by older versions. New fields go into new columns or are dropped on read through `PydanticJSON(read_upgrade=...)`, never added to the stored approval or snapshot data.
- **Key files:**
  - `app/services/questions.py` (classifier)
  - `app/services/answers.py` (questions, answers, bank, `measure`)
  - `app/services/package.py` (approval, review state, snapshots)
  - `app/services/checklist.py`
  - `app/services/profile.py`, `app/services/profile_form.py`
  - `app/routes/*`, `app/templates/*`, `app/static/style.css`
- **Regression tests:**
  - `tests/test_qa_regressions.py`
  - `tests/test_answer_review_regressions.py`
  - `tests/test_review_upgrade.py`
  - `tests/test_tracking_review_regressions.py`
  - `tests/test_assessment_review_regressions.py`

## Still open

- **Windows:** documented and fixed in code, but not yet run on a Windows machine.
- **Live AI:** not yet tested with a real OpenRouter key and model.
- **"Graduation year" questions:** answered by the user, since the profile stores a date.
- **The MVP canvas is private:** share it before teammates can open it.

## Running it

```bash
git clone https://github.com/Varunsai1930/ResuApply.git
cd ResuApply
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python -m app
```

Then open http://127.0.0.1:8000. For Windows, see the README. For the AI screens without a key: `RESUAPPLY_DATA_DIR=data/demo .venv/bin/python -m scripts.fake_ai_server`.

## Prompt to start the next session

> I'm continuing work on ResuApply (this repo). Read `handsoff.md` first: it has the current state, the working agreements (one commit per change, a regression test per change, push only when I ask) and the MVP implementation plan. The MVP design is the canvas https://claude.ai/artifact/5a5nSzeR8bhJA7Q2uX7st8. Read its screens (especially "3 · Job workspace", "7 · Questions" and "8 · Submit and track") to match the layout and the four status meanings.
>
> Start with plan steps 1 and 2: the shared status helper and header partial, then the job workspace as five guided steps with a `next_action` function. Before coding, run `git fetch` and `git status`, run the test suite, and tell me your plan for step 1 in a few lines. Keep the existing behaviour and tests passing, add tests for each step, and commit each step separately. Don't push until I ask.
