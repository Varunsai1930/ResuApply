# ResuApply: final handoff

Updated 2026-10-18, the last day of the project. Repository: [Varunsai1930/ResuApply](https://github.com/Varunsai1930/ResuApply), branch `main`.

## Where things stand

- **Everything planned is built and on `main`.** The MVP plan, the V2 features in [PLAN.md](PLAN.md) and several additions beyond it (below).
- The full test suite passes: **1,730 tests** (`.venv/bin/python -m pytest`, about 80 seconds).
- **Windows CI passes** on every push and pull request (`.github/workflows/windows-validation.yml`).
- **Live AI check passed.** On 17 October 2026 every AI stage (requirements, evidence suggestions, resume tailoring, answer drafts, cover letters) produced results that passed the local checks with `nvidia/nemotron-3.5-super-vl:free`. The free tier also timed out at 90 s or returned provider errors on some requests and allows about 50 free-model requests a day, so a failed stage is simply retried.
- **Autofill** for forms hosted by Greenhouse, Lever and Ashby passes end to end against local copies of each form, and was run read-only on live forms of all three. It has not yet filled a live form.
- The app starts with one command: `./start.sh` (macOS, Linux) or `start.bat` (Windows).

[IMPLEMENTATION_AUDIT.md](IMPLEMENTATION_AUDIT.md) has the "Implementation remaining" table with evidence for each item.

## What's left

1. **Fill one live form.** Pick a real Greenhouse, Lever or Ashby posting, use **Fill the verified fields**, check every value in the browser, and **don't click Submit**. This is the one validation step not yet done. It runs a visible browser, so do it on your own machine.
2. **Real resumes.** The import is tested on a corpus of synthetic layouts. Try a few real PDF and Word resumes (two-column ones especially) and keep them on your machine.
3. **Unmerged Greenhouse deadline fix.** The local branch `greenhouse-import` (also on GitHub) has four commits that never reached `main` (`4fd3f16` to `54d0d24`). They hold the whole fetch to its deadline in the network layer, so a slow but healthy Greenhouse answer gets the full 10 seconds. On `main` the limit is effectively about 7 seconds, because a body read only starts while a full 3-second wait still fits before the deadline (`PHASE_SECONDS` in `app/services/greenhouse.py`). Greenhouse normally answers in under a second, so this rarely matters. Either rebase those four commits onto `main` (review them and run the tests, since `main` has moved a long way) or delete the branch.
4. **Branch cleanup.** The other old branches are merged or superseded:
   - `resume-import-hardening`: its fixes reached `main` as rewritten commits (for example `f3dccc2`, `51b6655`).
   - `backup/windows-ci-diag`: a diagnostic branch from the Windows CI hang, with a commit marked "to be dropped".
   - `background-ai-alembic`, `greenhouse-autofill`, `fix/windows-ci-hang`: merged through PRs #1 to #3.
   - `claude/strange-roentgen-ee0f02`: a session worktree branch.

   Delete them locally and on GitHub once item 3 is settled.
5. **Share the MVP canvas** if teammates should see it: [ResuApply MVP](https://claude.ai/artifact/5a5nSzeR8bhJA7Q2uX7st8) (private; share it from its Share menu).

**Decided, not pending:**
- **Workday autofill** is not planned: its sign-in and multi-step forms fall outside what deterministic autofill can verify.
- **A bundled desktop app** (with Tesseract and a browser) is not built; the start scripts cover setup.
- **Sharing rules with ResuSkill** as one package would need ResuSkill restructured, which is its owner's decision. `scripts/rules_parity.py` lists where the two differ.

## Working agreements

- **One commit per change.** Each fix or feature step gets its own commit as soon as it passes tests. (Also saved as a Claude memory.)
- **Push only when asked.** Commits stay local until the push is confirmed.
- Every fix gets a regression test, checked to fail without the fix.
- Commit messages explain the problem and the fix, and end with the `Co-Authored-By` line.
- **Review before merging.** Branches from other sessions are reviewed (and their tests run) before they go into `main`.
- Run `git fetch` and `git status` before starting, and re-run the tests after pulling.

Cloud sessions couldn't push to this repo, because their GitHub connection wasn't linked to the `Varunsai1930` account. They handed over git bundles instead (`git bundle verify`, then `git fetch <bundle> <branch>:<branch>`). To let them push directly, reconnect GitHub as `Varunsai1930` at https://claude.ai/customize/connectors and install the Claude GitHub App on the repo.

## What was built since the last handoff (11 October)

Before that: the MVP (guided job workspace, jobs board, follow-up reminders, visible trust in resume review, profile gaps, answer bank, onboarding with sample data, light and dark mode), resume import, graduation answers and Greenhouse job import. Since then:

- **Reliability audit and fixes** (`e5838af`..`d136632`): invalid Unicode refused and legacy text recoverable, damaged DOCX handled, unsaved-edit guards, a hard AI deadline with bounded responses, malformed numbers rejected, local acceptance script (`scripts/smoke_local.py`).
- **Resume layouts, Word export and opt-in local OCR** for scanned PDFs (`6592733` and follow-ups).
- **Checklist counts** ("3 of 4 required items met"), never a score.
- **Cover letters**, optional, checked against cited profile facts like every other AI output.
- **New interface** around a top navigation bar.
- **Alembic migrations** (`app/migrations`), with a backup before each upgrade (PR #1).
- **Background AI requests** with a self-refreshing progress page (PR #1).
- **Autofill** in a visible Playwright browser that never clicks Submit: Greenhouse first (PR #2), then the approved cover letter, then Lever and Ashby.
- **Windows CI fix** (PR #3): the hang was a 10 MB test parameter id; every test now has a 120-second timeout.
- **Live OpenRouter run**, which led to two fixes: a cited entry's own dates now count as support, and the cover letter prompt avoids number words and unsourced skills.
- **ResuSkill parity**: held, in-progress and levelled credentials are told apart as ResuSkill does; `scripts/rules_parity.py` reports the rest.
- **One-command start** (`start.sh`, `start.bat`) and docs that lead with the promise: every AI claim must cite the profile, and anything it can't back is rejected before the user sees it.

## How the app works (for the next person)

- **Startup:** `python -m app` runs `load_settings`, `check_storage` and `check_port`, then the lifespan in `app/main.py` runs the Alembic upgrade and the stored-data upgrades (`upgrade_stored_questions`, `rewrite_approvals_with_stored_rules`, `upgrade_stored_approvals`).
- **Middleware, outermost first:** `NoFramingMiddleware`, `TrustedHostMiddleware`, `SameOriginMiddleware`, `HeadAsGetMiddleware`, `ControlCharacterMiddleware` (URL-encoded forms only).
- **Approval rules:** to make approval checks stricter, raise `APPROVAL_RULES` in `app/services/package.py`. An approval is trusted only when `approval_rules_for` equals its `approval_key`.
- **Schema changes:** add an Alembic migration in `app/migrations`. Keep the database readable by older versions: new data goes into new nullable columns or is dropped on read through `PydanticJSON(read_upgrade=...)`.
- **Outbound requests:** only OpenRouter (approved, reduced context), Greenhouse's job board (on Import) and the employer's form in the autofill browser (only when the user opens it). Tests never use the network: `create_app(ai_transport=..., greenhouse_transport=...)`, and the autofill tests answer every browser request locally.
- **Key files:**
  - `app/services/claims.py`, `citations.py` (what an AI claim may say and must cite)
  - `app/services/questions.py`, `answers.py` (classifier, factual values, answers, bank)
  - `app/services/package.py` (approval, review state, snapshots)
  - `app/services/cover_letter.py`, `resume.py`, `resume_layouts.py`, `resume_docx.py`
  - `app/services/resume_import.py`, `resume_draft.py`, `resume_ocr.py`
  - `app/services/greenhouse.py`, `postings.py`
  - `app/services/autofill.py`, `autofill_plan.py`, `autofill_browser.py`, `resume_attachment.py`
  - `app/services/status.py`, `workspace.py`, `board.py`
  - `app/routes/*`, `app/templates/*`, `app/static/style.css`
- **Scripts:** `smoke_openrouter.py` (live AI check), `smoke_local.py` (CLI acceptance), `fake_ai_server.py` (AI screens without a key), `rules_parity.py`, `ensure_packages.py` (used by the start scripts).

## Running it

```bash
git clone https://github.com/Varunsai1930/ResuApply.git
cd ResuApply
./start.sh
```

Then open http://127.0.0.1:8000 (the script opens it for you). On Windows run `start.bat`. For development: `.venv/bin/pip install -r requirements-dev.txt`, then `.venv/bin/python -m pytest`. For autofill: `.venv/bin/pip install -r requirements-autofill.txt` and `.venv/bin/python -m playwright install chromium`. For the AI screens without a key: `RESUAPPLY_DATA_DIR=data/demo .venv/bin/python -m scripts.fake_ai_server`.

## Prompt to start a future session

> I'm picking up ResuApply (this repo) after the project ended. Read `handsoff.md` first: it has the current state, the working agreements (one commit per change, a regression test per change, review before merging, push only when I ask) and what's left. Before starting, run `git fetch`, `git status` and the test suite. Don't push until I ask.
