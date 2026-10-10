# ResuApply: session handoff

Session date: 2026-10-10. Repository: [Varunsai1930/ResuApply](https://github.com/Varunsai1930/ResuApply), branch `main`.

## Where things stand

- `main` on GitHub ends at `86ed43e`. The local copy matches it, with nothing uncommitted except this file.
- The full test suite passes: **825 tests** (`.venv/bin/python -m pytest`). It was 777 at the start of the session.
- Four code reviews were run at high effort. All 17 findings across them are fixed, committed and pushed.
- The app is ready for friends and teammates to run locally with the manual workflow. AI features have only been tested with a mocked provider. Live testing with a real OpenRouter key is still pending.

## Working agreements from this session

- **One commit per change.** Each fix gets its own commit as soon as it passes tests. (Also saved as a Claude memory.)
- **Push only when asked.** Commits stay local until the push is confirmed.
- Commit messages explain the problem and the fix, and end with the `Co-Authored-By` line.

## What happened, in order

### 1. "Is the website ready to use?" and the first review

- The README's setup steps are correct for macOS and Linux. At that point all the work since `b2e9fcc` (26 files and 5 new test files) was uncommitted, so anyone cloning would have got the older version.
- The first review of that uncommitted work found 8 issues:
  1. The question classifier no longer recognised common form labels ("Mobile phone", "Phone Number (with country code)", "Full legal name", "Expected graduation date (MM/YYYY)"…), and "What is your GPA? (out of 4.0)" was treated as an open question.
  2. Requirements editor: after a "changed while you were editing" error, one more click on Save overwrote the newer requirements unseen.
  3. Job edit form: after the same error, Save failed every time, and the only way out discarded what the user typed.
  4. Every read re-checked the question category, so older saved questions silently lost their profile value.
  5. `review_state` ran the full package check on every call.
  6. Evidence suggestions re-hashed the whole profile once per suggestion.
  7. Changing the evidence-suggestion cache key hid suggestions saved before the update.
  8. The two editors read the saved revision in two different ways.

### 2. UI/UX mentoring advice (advice only, nothing implemented)

1. Turn the job page into a step-by-step flow with a progress bar and one "Next" button.
2. Fix the first 10 minutes: a "Load sample profile and job" button, PDF or DOCX resume import (the V2 plan), and auto-filled title and company.
3. Make the trust features visible: hovering a proposed bullet highlights the source lines it came from, with a word-level diff.
4. Group the ~20 status labels into about four meanings, each with one colour and one icon.
5. Show "here's what changed" when a save conflicts, instead of an error.
6. Use htmx so a click updates only its section instead of reloading the whole page. React isn't needed.
7. Make the tracker worth opening daily: a board view, follow-up reminders and response rates.
8. A one-command start, GitHub issue templates, and watching two or three friends use the app without helping them.

### 3. Fixes and README, then the first push (`7a0d116`)

- **Classifier:** common labels added. Trailing format hints are ignored only when they are format hints: "(MM/YYYY)", "(optional)", "(with country code)", "(out of 4.0)" or "(City, State)". Other bracketed text, such as "Name (of your reference)", leaves the question unrecognised. "Graduation year" is still answered by the user, because the profile stores a date.
- **Editor conflicts (job and requirements):** the page keeps what the user typed and shows the version saved now. Saving again unchanged is refused again. A **"Save mine over it"** box (**"Save mine over them"** in the requirements editor) saves the user's version over the newer one. Both editors read the revision through `reviewed_revision_of` in `app/routes/jobs.py`.
- **README:** a new Windows section (`py -3.12`, calling `.venv\Scripts\python` directly, PowerShell and Command Prompt). Windows has not been tested yet; the README asks people to open an issue. The README also explains the conflict behaviour and the field-matching rules.
- Committed together with the earlier uncommitted work as `7a0d116`, then pushed.

### 4. The remaining four findings (`2192245`, `e5092f5`, `eb44bc8`, `28dd92e`)

- The profile is hashed once per suggestion view.
- Suggestions stored under the old cache key are still shown and reused when they belong to the same job.
- Approvals record the version of the approval rules they passed, so only older approvals get re-checked.
- Saved questions are upgraded once at startup (`upgrade_stored_questions`), and reading answers trusts the stored category and field again.

### 5. Second review: 3 findings (`e4b9967`, `5f26782`, `2809cff`)

- **Confirmed bug:** after the upgrade reclassified a question as sensitive, a profile value stored by "Use the profile value" still counted as its answer. Fixed by dropping that value when the category changes.
- The rules version lived inside the approval data, which older versions of the app reject. It moved to its own column, `applications.approval_rules`.
- Approvals made before the update are checked once at startup (`upgrade_stored_approvals`).

### 6. Third review: 4 findings (`6332102`, `4f95603`, `05f7a8a`, `c6d2466`)

- **Confirmed across versions:** the "already checked" mark outlived the approval it was made for, so an approval recorded by the older code could skip the stricter check. It is now tied to that specific approval (`approval_rules_for` holds its key, from `approval_key`). Verified by approving with a checkout of `b2e9fcc` in between.
- The startup upgrade keeps what the user wrote or chose (their own and bank answers, and skips). It removes only profile values and AI drafts.
- The old `rules` field is ignored only when reading from the database, through `PydanticJSON(..., read_upgrade=...)`. New code that passes it gets an error.
- The startup approval check only looks at rows that have an approval.

### 7. Fourth review: 2 findings (`58a211f`, `86ed43e`)

- **Confirmed:** an answer kept for a question moved to **Confirm category** was never shown, and choosing a category deleted it. The row now shows the kept answer, and leaving Unrecognized keeps the user's own answers.
- Approvals the startup check finds not reading Approved are marked as seen with `seen:<key>`, so later starts skip them. That mark never counts as "passed".

## How it works now (useful for the next person)

### Startup upgrades

These run in `app/main.py`'s lifespan, in this order, each committing its own changes:

1. `answers.upgrade_stored_questions`: stores each saved factual question as the current rules read it.
   - If the rules still read it as factual, its field follows them (for example `name` becomes `first_name`).
   - If they read it as sensitive, it becomes sensitive.
   - Otherwise it becomes Confirm category.
   - When a category changes, profile values and AI drafts are removed and the user's own answers are kept. The job goes back to Draft.
2. `package.rewrite_approvals_with_stored_rules`: removes the old `"rules"` field from stored approvals and submitted packages.
3. `package.upgrade_stored_approvals`: checks each approval made under older rules once.
   - If it reads Approved and still passes, it's marked as checked.
   - If it reads Approved and fails, it's removed and the job goes back to Draft.
   - Anything else is marked as seen.

### Approval rules version

- To make approval checks stricter in future, raise `APPROVAL_RULES` in `app/services/package.py` and note what changed in the comment above it.
- Every approval is then re-checked once at the next start.
- `_checked` trusts an approval only when `approval_rules_for` equals that approval's `approval_key`.

### Compatibility

- The database stays readable by earlier versions: new data goes into new columns, not inside the stored approval data. This was verified by reading a new database with the code from `b2e9fcc`.
- Keep it that way: don't add fields to the stored approval and package data that older code would reject.

### Key files

- `app/services/questions.py`: classifier (`_FIELD_LABELS`, `_TRAILING_HINT`, `_field_request`).
- `app/services/answers.py`: question upgrade, `set_category`, `_drop(keep_own=...)`.
- `app/services/package.py`: approval, review state, rules stamp, startup approval upgrades.
- `app/routes/jobs.py` and `app/routes/requirements.py`, with `job_form.html` and `requirements_form.html`: the editor conflict flows.
- `app/ai/operations.py`: evidence suggestions and cache keys (`_suggestion_keys`, `_own_run`).
- Regression tests: `tests/test_answer_review_regressions.py`, `tests/test_review_upgrade.py`, `tests/test_tracking_review_regressions.py`, `tests/test_assessment_review_regressions.py`, `tests/test_ai_operations.py`.

## Still open

- **Windows** setup is documented but not yet tested on a Windows machine.
- **Live AI**: OpenRouter has not been tested with a real key and model. Each person needs their own key, and free models can change.
- An out-of-date approval made before this update, and never checked, is still visited once more at the first start after this update (then marked as seen).
- "Graduation year" questions are answered by the user (the profile stores a date, not a year).
- None of the UI/UX advice in section 2 has been implemented. Suggested order: sample data, then resume import, then the step-by-step flow, then htmx.

## Running it

```bash
git clone https://github.com/Varunsai1930/ResuApply.git
cd ResuApply
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/python -m app
```

Then open http://127.0.0.1:8000. For Windows, see the README's Windows section.
