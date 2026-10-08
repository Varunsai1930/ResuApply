# ResuApply

A local job application copilot web app. It turns your verified profile and a pasted job description into an evidence checklist, a tailored resume, reviewed answers and an application record, **without inventing anything**.

> **Status:** Milestones 1 (Foundation), 2 (Assessment), 3a (Tailoring) and 3b (Complete V1) are built: profile, jobs, tracker, requirements, evidence, source-backed resume proposals, A4 print export, questions and answers, the answer bank, package approval and submitted snapshots. AI features have passed mocked-provider and browser checks; live OpenRouter validation remains pending until a key is configured. V2 (PDF/DOCX import, Greenhouse) is not started; see [PLAN.md](PLAN.md). The same workflow is already available as an agent skill for Claude Code and Codex: [ResuSkill](https://github.com/Varunsai1930/ResuSkill).

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

Requires Python 3.12. These are the commands used to verify the app on macOS.

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
- **Tailoring a resume** uses that same approved, reduced context plus the job and requirements. Removed items cannot be selected or cited, and each rewrite is checked against both the original profile and the exact text shared. Sharing approval and input freshness are checked before each request, including the corrective retry.

To check requirement extraction, evidence suggestions and resume tailoring with a key and model using fictional data (a throwaway database; your `data/` is untouched):

```bash
.venv/bin/python -m scripts.smoke_openrouter
```

To try the AI screens without a key, run the app against a simple local stand-in for the model (nothing leaves your machine):

```bash
RESUAPPLY_DATA_DIR=data/demo .venv/bin/python -m scripts.fake_ai_server
```

### Prepare and export a resume

Open a job's **Resume** section. **Use profile as-is** creates a proposal without an API key. With an AI key, **Tailor with AI** uses the approved sharing context; a free model first takes you to the editable sharing preview when approval is needed.

Review the original sources and proposed wording side by side, then choose **Accept resume**. Employers, titles, dates, degrees, contact details and skill labels come from your saved profile. Automated checks reject unknown or cross-entry sources, unsupported numbers and years (including number words and scale suffixes), technologies, credentials and skills. You still need to review meaning: citations and token checks cannot prove that a paraphrase is accurate.

**Print / save PDF** opens the fixed, single-column template of the accepted resume. Choose **Print / save PDF** again, select A4, and turn off browser headers and footers. Long profiles continue onto more pages. Both a one-page profile and a nine-page synthetic profile were checked in Chrome for content retention and unclipped output.

Regenerating creates a separate proposal and leaves the accepted resume available for printing. Profile or job changes mark old proposals and accepted resumes stale; create and accept a fresh proposal before printing. Resume acceptance leaves tracking unchanged.

### Answer questions, approve and record what you submitted

Open a job's **Questions** section and add the questions from the employer's form, one at a time, with **Required** (ticked by default) and an optional length limit in characters or words. Each question is sorted by fixed keyword rules into a category, shown beside it:

- **Factual** (name, email, school, graduation date and similar): filled from your profile every time and labelled **From profile**. If the profile has no value, the row links to **Profile** to add it; you can also answer it yourself.
- **Sensitive-factual** (work authorization, sponsorship): the profile value is shown but nothing is filled in until you press **Confirm this answer**. Until then the label is **User input required**. If the profile value changes later, you confirm again.
- **Sensitive** (gender, race, disability, salary, consent and similar): you write the answer yourself, or choose **Skip this optional question** when it is optional. These are never drafted by the AI model and never saved to the answer bank.
- **Open-ended** (why do you want this role, describe a project): write an answer, or choose **Draft open answers with AI**. Drafts use the same approved, reduced context as tailoring, cite the profile items they are built from, and are checked against your profile and the question's length limit. A draft is labelled **AI draft (pending review)** and is not an answer until you press **Accept draft** (or **Discard draft**); accepting checks it again against your current profile.
- **Unrecognized**: shown as **Confirm category** until you choose one with **Set category**. The app refuses changes that would let it answer a question it must not (for example, a sensitive question becoming factual). **Change category** and **Remove question** are under each row; removing a question never reuses its ID.

Labels you will see on a question: **From profile**, **AI draft**, **User answer**, **Missing information**, **User input required**, **Skipped (optional)**, **Confirm category** and **AI draft (pending review)**.

**Answer bank.** After you accept an answer to a non-sensitive question, **Save to answer bank** keeps it. The **Answers** page lists everything saved, with the job it came from, and **Delete from answer bank** removes an entry. When a similar question appears on another job, **Start from a saved answer** shows the saved text; **Use as starting point** only fills the answer box, and you still press **Save my answer**. Values filled from your profile are not saved to the bank.

**Review and approve.** The **Review** section shows the package's state: **Draft** (not approved, or changed since), **Approved**, or **Stale** (your profile or the job changed after you approved it). It lists what blocks approval (no accepted resume, a resume that no longer matches the profile, a required question without an accepted answer, an unresolved sensitive or unrecognized question) and what is only worth a look (an optional question left unanswered, drafts waiting for review, no requirements reviewed). **Approve package** appears when nothing blocks it. Approval covers only the accepted resume and the questions entered in the workspace, not any other questions on the employer's form, and it never marks the job Applied. Editing the resume, a question or an answer clears the approval.

**Record what you submitted.** Submit the application yourself on the employer's site. Then, under **Tracking**, choose **Applied**; while the package is Approved, **Save the approved package as what I submitted** is ticked by default. Recording it saves a **submitted package**: the job details and description, the resume exactly as rendered, the answers and the profile and job revisions, listed under **Submitted packages** in the history. Open one to see it as submitted, including **Open the resume as submitted** for a print view of the frozen resume. Later changes to your profile or the job never alter it, though they mark the unsubmitted package Stale. Choose Applied with the box unticked, or without an approved package, to record a plain status change.

## What's included

- **Profile:** a guided form for contact details, links, summary, education, experience, projects (with bullets and technologies), skills, skills you've confirmed you lack, certifications, preferences, availability and per-country work authorization (yes / no / unknown). Every save goes through a **Review changes** step that lists each added, changed or removed fact. Entries, bullets and certifications get stable IDs (`exp-1`, `exp-1-b2`, `cert-1`) that survive edits, and deleted IDs are never reused. The revision increases only when something really changed. The validation and ID rules are ported from ResuSkill.
- **Jobs:** manual entry with title, company and pasted description (required), plus location and URL (optional). The description is stored exactly as pasted and is only ever shown as text. Changing the title, company, location or description increases the job revision.
- **Tracker:** every job with its review state (Draft, Approved or Stale, recalculated whenever the list is opened) and tracking status (Saved, Applied, Assessment, Interview, Rejected, Offer, Withdrawn), a dated status history and notes. Only you change the status; approval never sets Applied.
- **Job Workspace:** the job's details and description, tracking and notes, the requirements checklist and resume workflow, questions and answers, review and approval, and any submitted packages.
- **Requirements:** enter them yourself or have the AI model propose them; either way you review and correct each one before saving. Every requirement quotes words from the description, and if any excerpt can't be found there, nothing is saved. Each can have a comparable criterion (skills, degree, graduation window, location/work mode, work authorization, start date, years of experience). Requirement IDs (`r1`, `r2` …) stay stable across edits and are never reused.
- **Checklist:** Python compares each requirement with your profile and shows **Met**, **Unmet** or **Unknown** with the reason, grouped as sources, gaps and unknowns. A skill that isn't in your profile is Unknown unless you confirmed you lack it; anything that can't be compared stays Unknown until you link evidence. Gaps stay visible but never block an application.
- **Evidence and overrides:** link profile bullets or entries as evidence yourself, or ask the AI model for suggestions and accept or reject each one. Suggestions change nothing until you accept them. You can override any status, and the override is always shown with your reason and the calculated status.
- **Resume:** manual or AI proposals, original/proposed comparisons with source IDs, claim checks, separate accepted content, stale-input checks and browser Save as PDF. Regeneration and provider failures preserve accepted work.
- **Questions and answers:** categories by fixed rules, profile-backed answers, your own answers, and AI drafts only for open-ended questions, each reviewed and accepted by you. Sensitive questions are never drafted or banked.
- **Answer bank:** the **Answers** page, with similar-question suggestions that only fill the answer box.
- **Review and approval:** Draft, Approved or Stale, listed blockers and warnings, and an explicit **Approve package** step that never sets Applied.
- **Submitted packages:** the approved package frozen when you record Applied, viewable (including its resume) after any later profile or job edit.

Evidence confirmations apply to the source content you reviewed. If a linked source changes, the checklist asks you to review and reconfirm it before it counts as supporting evidence again. Evidence saved before content checks were added also needs one explicit reconfirmation; existing links remain available to review or remove. Unrelated contact edits keep unchanged evidence valid.

Profile saves reject overlapping edits against an older revision. Dates support `YYYY`, `YYYY-MM` and valid `YYYY-MM-DD` calendar dates, with month lengths and leap years used in comparisons. Work-authorization country codes and recognized English country names are normalized to the same two-letter code.

## Project layout

```text
app/
  main.py         FastAPI app factory (startup creates tables)
  config.py       Settings from the environment / .env
  db.py           SQLAlchemy engine, sessions, Pydantic-validated JSON columns
  models.py       candidates, jobs, applications, answer_bank
  schemas/        Pydantic shapes for profile, resume and tracking JSON
  ai/             OpenRouter client, prompts, requirements, evidence, resume tailoring and answer drafts
  services/       Rules ported from ResuSkill (profile, requirements, checklist, claims),
                  outbound context, resume proposals/acceptance, questions and answers, package approval and snapshots,
                  form parsing, jobs, tracking
  routes/         Profile, jobs, resume, questions, package/snapshot and answer bank pages
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

Python 3.12 · FastAPI · Jinja2 · SQLAlchemy · SQLite · Pydantic · HTTPX → OpenRouter (model configurable). No application browser-automation dependency before V2.

## License

[MIT](LICENSE)
