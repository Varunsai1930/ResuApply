# ResuApply

A local job application copilot web app. It turns your verified profile and a pasted job description into an evidence checklist, a tailored resume, reviewed answers and an application record, **without inventing anything**.

> **Status:** Milestones 1 (Foundation), 2 (Assessment), 3a (Tailoring) and 3b (Complete V1) are built: profile, jobs, tracker, requirements, evidence, source-backed resume proposals, A4 print export, questions and answers, the answer bank, package approval and submitted snapshots. AI features have passed mocked-provider and browser checks; live OpenRouter validation remains pending until a key is configured. PDF/DOCX profile import (read locally, by layout rules, with optional local OCR), Classic/Modern/Compact resume layouts, editable DOCX resume export and Greenhouse job import (fetched only when you choose Import) are built. Limited Greenhouse autofill (optional, with Playwright) is built and tested against local Greenhouse-like forms; a manual check on a live hosted form remains pending. See [PLAN.md](PLAN.md). The same workflow is already available as an agent skill for Claude Code and Codex: [ResuSkill](https://github.com/Varunsai1930/ResuSkill).

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
- **Local first.** Runs on `127.0.0.1` with SQLite. Only the reduced career context you approve is sent to the configured model. The only other request the app makes is a Greenhouse job import, and only when you choose **Import**: it asks Greenhouse's public job board for that one posting and sends nothing about you.

## Run it

Requires Python 3.12 and Git. The app keeps its Python packages in a `.venv` folder inside the project and its data in `data/`. Optional OCR also needs a local Tesseract installation, described below.

### macOS and Linux

These are the commands used to verify the app on macOS. Linux uses the same ones.

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

### Windows

Windows setup commands are provided below. Native Windows acceptance remains pending. A Windows validation workflow is prepared in `.github/workflows/windows-validation.yml`; it runs the tests and acceptance checks on a Windows runner once the workflow is available in GitHub. If a step fails, please [open an issue](https://github.com/Varunsai1930/ResuApply/issues) with the command and its output.

1. Install **Python 3.12** from [python.org](https://www.python.org/downloads/windows/) (keep **py launcher** selected in the installer), or run `winget install Python.Python.3.12`. Install [Git for Windows](https://git-scm.com/download/win) if you don't have it.
2. In **PowerShell** or **Command Prompt**, run:

```powershell
git clone https://github.com/Varunsai1930/ResuApply.git
cd ResuApply
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
copy .env.example .env
.\.venv\Scripts\python.exe -m app
```

Then open <http://127.0.0.1:8000>. Stop the server with Ctrl+C.

Run the tests and the local acceptance check from the project folder:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m scripts.smoke_local
```

The acceptance check starts the real localhost server using fictional data in a temporary folder. It verifies imports, reviewed profile saving, resume acceptance and print HTML, port conflicts, shutdown and persistence after restart. It needs no API key. On macOS or Linux, use `.venv/bin/python -m scripts.smoke_local`.

The prepared GitHub Actions workflow also checks Chromium PDF output with CI-only browser dependencies. Runtime dependencies are unchanged. GitHub documents the Python setup and Windows runner configuration in its [Python Actions guide](https://docs.github.com/en/actions/tutorials/build-and-test-code/python). A prepared workflow is not evidence that Windows verification has passed.

To run the same optional PDF check locally on Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install playwright==1.63.0
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe -m scripts.smoke_local --output-dir validation-results --print-pdf validation-results\accepted-resume.pdf
```

These extra browser packages are needed for acceptance checks, not for running the app. The output contains fictional data only; the temporary application database is removed after the check. Once the workflow is present on GitHub's default branch, **Actions → Windows acceptance → Run workflow** runs these checks without requiring a Windows machine locally.

Notes for Windows:

- The commands call `.\.venv\Scripts\python.exe` directly, so you don't need to activate the environment. This also avoids PowerShell's script execution policy, which blocks `Activate.ps1` by default.
- `py -3.12` picks Python 3.12 even if another version is your default. If `py` isn't found, reinstall Python with the py launcher option, or use the full path to `python.exe`.
- With [uv](https://docs.astral.sh/uv/): `uv venv --python 3.12 .venv`, then `uv pip install --python .venv\Scripts\python.exe -r requirements-dev.txt`.
- Edit `.env` in any text editor (for example `notepad .env`) to add your OpenRouter key.
- To start over, stop the app and delete `data\resuapply.db`.
- Wherever this README sets a variable in front of a command (for example `RESUAPPLY_DATA_DIR=data/demo` for the local AI stand-in), set it first. In PowerShell: `$env:RESUAPPLY_DATA_DIR = "data\demo"`. In Command Prompt: `set RESUAPPLY_DATA_DIR=data\demo`. Then run the command with `.venv\Scripts\python` in place of `.venv/bin/python`. The variable lasts until you close that window.

### Configuration

Every setting is optional and is read from the environment or `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `RESUAPPLY_PORT` | `8000` | Port to listen on |
| `RESUAPPLY_DATA_DIR` | `data` | Folder for the SQLite database (`resuapply.db`); relative paths start at the project root |
| `OPENROUTER_API_KEY` | *(empty)* | Your [OpenRouter](https://openrouter.ai) key. Without it the AI buttons are hidden and everything else works |
| `OPENROUTER_MODEL` | `nvidia/nemotron-3.5-lightning:free` | Any OpenRouter model ID that supports tool calling |
| `OPENROUTER_MODEL_TRUST` | `free` | `free`: you approve what is sent before the first AI request that includes your career history. `trusted`: that review is optional |
| `RESUAPPLY_AUTOFILL_BROWSER` | *(empty)* | Optional Greenhouse autofill: path to a Chrome or Chromium to open instead of Playwright's own |

The bind address is always `127.0.0.1` and cannot be configured. The app also rejects requests whose `Host` isn't `127.0.0.1` or `localhost`, and form posts coming from other websites.

Tables are created on startup, and columns added by a later milestone are added to an existing database automatically (nothing is renamed or dropped). To start over, stop the app and delete `data/resuapply.db`. The database, `.env`, uploads and generated files are git-ignored.

### Optional OCR for scanned resumes

Install **Tesseract with English (`eng`) language data**, and make its `tesseract` command available on `PATH`. On macOS, use `brew install tesseract`; on Debian/Ubuntu, use `sudo apt install tesseract-ocr tesseract-ocr-eng`. Windows installers and other platforms are listed in [Tesseract's installation guide](https://tesseract-ocr.github.io/tessdoc/Installation.html). Restart the app after changing `PATH`. `tesseract --list-langs` should include `eng`.

On **Profile → Import resume**, tick **Use OCR for scanned pages** before importing a scanned PDF. OCR runs locally and uses no API key. Normal selectable-text pages keep their existing extraction; pages with a scanned body use OCR even when their contact header is selectable. Review all recognized text, especially names, dates and numbers, before saving. Without Tesseract, ordinary text PDF and DOCX import still work, and requesting OCR displays a setup message.

Imports keep the existing 10 MB, first-30-pages and 100,000-character limits. OCR has a 60-second total deadline, bounded page images and one active OCR import at a time. Failed, interrupted or unreadable OCR imports leave the saved profile unchanged.

### Optional Greenhouse autofill

Autofill opens a job's Greenhouse application form in a visible browser window and fills in what your approved package covers. It needs [Playwright](https://playwright.dev/python/), an optional add-on pinned in `requirements-autofill.txt` (`requirements-dev.txt` includes it for the tests):

```bash
.venv/bin/pip install -r requirements-autofill.txt
.venv/bin/python -m playwright install chromium
```

On Windows, use `.\.venv\Scripts\python.exe -m pip install -r requirements-autofill.txt` and `.\.venv\Scripts\python.exe -m playwright install chromium`. Restart the app afterwards. Without Playwright the app starts and everything else works; the autofill card shows these setup steps instead. Autofill needs a desktop session that can show windows.

**Using it.** On a job's **Submit and track** step, the **Fill the Greenhouse form** card lists what autofill needs:

1. The job's link is a posting on `boards.greenhouse.io` or `job-boards.greenhouse.io` (an imported job has one; otherwise add it with **Edit details**).
2. The package is **Approved** and current.
3. The resume PDF you saved from that package is attached: open the accepted resume, choose **Print / save PDF**, then **Attach PDF**. It must be a readable PDF of up to 10 pages and 10 MB, without a password, showing your name as the approved resume does. It is kept in `data/attachments/` and belongs to the package you approved: when the package changes, attach it again.

**Open the form in a browser** opens the form and reads it; nothing is filled yet. The card lists every field with what ResuApply would do and why. **Fill the verified fields** fills only those, reads each value back and shows the result: **Filled and read back**, **Attached and checked**, **Doesn't match** (with what the form shows, for example when the page reformats a phone number or rejects the file), **Kept the existing value**, **Already filled** or **Left for you**. Then check every field in the browser, answer the rest and click Submit there yourself. Record Applied under **Tracking** afterwards; autofill never changes the status. **Close the browser** closes it; stopping the app closes it too.

**What it fills.** Only text, email, phone and link fields and text boxes with exactly one label, when the label is a contact field from your approved resume (name, email, phone, location, LinkedIn, GitHub, website) or is word for word a question in your approved package with an accepted, non-sensitive answer; plus the resume upload. A first- or last-name field is filled only from your own answer to that question, since your full name is never split. A value with line breaks never goes into a one-line field, and one longer than the field allows is left out.

**What it leaves for you.** Sensitive questions (work authorization, sponsorship, self-identification, demographics, salary and the like, including anything in a self-identification section), consent and declaration boxes, drop-downs, search fields, tick boxes, choices, dates, other uploads such as a cover letter, unlabelled fields, fields sharing a label, questions you skipped and anything not in your package. A field that already holds a different value is kept unless you tick **Replace** for it, and only if it still holds what the card showed.

**When it stops.** If the page shows a CAPTCHA or a sign-in, moves to another site, or its fields change after it was read, ResuApply fills nothing more and says the browser is yours; **Check the form again** reads it afresh when you want to continue. It never clicks a button or sends key presses, and while it fills it stops any submission the page itself attempts and tells you. The browser starts without saved cookies or sign-ins. The AI model plays no part: autofill uses only your reviewed data and fixed rules.

**Limits.** Only standard Greenhouse-hosted forms are supported; an employer's own careers page, a Greenhouse form embedded in another site and other platforms are filled in by hand. Because employers add their own questions and widgets, support is defined by the fields ResuApply can verify, not a promise to fill every form.

### AI model

The model is a setting, and the app never switches it on its own. Free models can change availability, limits or tool-calling support at any time; if the configured one stops working, the app reports the provider's error, keeps your work and the manual workflow keeps going. Changing `OPENROUTER_MODEL` is always your decision.

- Each request offers one tool that only returns data in a fixed schema. The result is validated locally; malformed or invalid output gets one corrective retry, then an error is shown. Requests time out after 90 seconds.
- Validated results are stored and reused while the inputs, model and prompt revision are unchanged. The app records the model, prompt revision, input revisions, time and token usage. Logs never contain your key, prompts, answers or any profile or job text.
- **Extracting requirements** sends only the job's title, company, location and description.
- **Suggesting evidence** sends a reduced copy of your career history: never your contact details, links, work authorization, preferences, availability, GPA, job locations or skills you said you lack. With a `free` model you see an editable preview at **Profile → What the AI model may see** and approve it first; you can leave out or reword any item. The approval is reused until a profile change alters what would be sent. Redaction is best effort.
- **Tailoring a resume** uses that same approved, reduced context plus the job and requirements. Removed items cannot be selected or cited, and each rewrite is checked against both the original profile and the exact text shared. Sharing approval and input freshness are checked before each request, including the corrective retry.

To check requirement extraction, evidence suggestions, resume tailoring and answer drafting with a key and model using fictional data (a throwaway database; your `data/` is untouched):

```bash
.venv/bin/python -m scripts.smoke_openrouter
```

On Windows, use `.\.venv\Scripts\python.exe -m scripts.smoke_openrouter`. A missing key returns exit code 2 and makes no provider request. Live provider validation remains pending until this check succeeds with the configured model; the ordinary test suite exercises all four stages with a local stand-in.

To try the AI screens without a key, run the app against a simple local stand-in for the model (nothing leaves your machine):

```bash
RESUAPPLY_DATA_DIR=data/demo .venv/bin/python -m scripts.fake_ai_server
```

### Prepare and export a resume

Open a job's **Resume** section. **Use profile as-is** creates a proposal without an API key. With an AI key, **Tailor with AI** uses the approved sharing context; a free model first takes you to the editable sharing preview when approval is needed.

Review the original sources and proposed wording side by side, then choose **Accept resume**. Employers, titles, dates, degrees, contact details and skill labels come from your saved profile. Automated checks reject unknown or cross-entry sources, unsupported numbers and years (including number words and scale suffixes), technologies, credentials and skills. You still need to review meaning: citations and token checks cannot prove that a paraphrase is accurate.

Choose **Classic**, **Modern** or **Compact** in the Resume section or print view, then **Apply layout**. All three use the same selected resume facts in a single reading column; Modern adds a navy accent and Compact reduces spacing. The choice is saved for that job. Changing it clears package approval and requires review again; applying the current choice leaves approval intact. A page showing an older package cannot change its layout.

**Print / save PDF** opens the accepted resume in your chosen layout. Choose **Print / save PDF** again, select A4, and turn off browser headers and footers. Long profiles continue onto more pages. Short and multi-page synthetic resumes were checked in Chromium for content retention and unclipped output across all three layouts.

**Download Word (.docx)** exports the current accepted resume as editable Word paragraphs, bullets and links in the chosen layout. It includes only the selected content, with employers, dates and other facts from your verified profile. Submitted packages also offer a Word download from their frozen content and layout, even after later profile edits. Editing a downloaded file does not update the app's package, so review any outside edits yourself before submitting.

Regenerating creates a separate proposal and leaves the accepted resume available for printing. Profile or job changes mark old proposals and accepted resumes stale; create and accept a fresh proposal before printing. Resume acceptance leaves tracking unchanged.

**Cover letter (optional).** In the Questions step, write the letter body yourself or choose **Draft a cover letter with AI**. A draft uses only the profile content you approved for sharing, cites the profile items behind it, and is refused if it states a number, technology or credential those items don't support (the job's own title and company name are fine). It isn't your letter until you **Accept draft**, which checks it again against your current profile. Your name, contact details, greeting and sign-off come from the profile and the job. An accepted letter is part of the package: changing it clears approval, a profile edit that removes its support blocks approval, and recording Applied freezes it with the package. **Print / save PDF** and **Download Word (.docx)** use the resume's layout.

### Answer questions, approve and record what you submitted

Open a job's **Questions** section and add the questions from the employer's form (a job imported from Greenhouse already has them), one at a time, with **Required** (ticked by default) and an optional length limit in characters or words, up to 10,000 (the longest answer ResuApply keeps). Each question is sorted by fixed rules into a category, shown beside it. Factual answers require a request for a specific field: mentioning email or university in a narrative question does not fill in contact or school details. A field label may carry a format hint such as **(MM/YYYY)**, **(optional)**, **(with country code)** or **(City, State)**; any other bracketed text, like "Name (of your reference)", leaves the question for you to categorize. Graduation questions are answered from your education end date: a **graduation year** question gets the year, and a **graduation date** question gets the month and year, as `2027-05` or in the format its label asks for, such as **(MM/YYYY)**. You answer it yourself when your profile's date is less precise than the question (only a year, or no day for **MM/DD/YYYY**), or when you have several degrees with different dates and the question doesn't name one ("Bachelor's degree graduation date" does). The row says which. Questions saved by an earlier version are re-read with these rules when the app starts: one that no longer asks for a profile field shows **Confirm category** (or becomes sensitive), one the rules had left Open-ended or Unrecognized that now asks for a profile field (such as a graduation year) becomes Factual unless you chose its category yourself, and that job's package goes back to Draft. What you wrote or skipped is always kept.

- **Factual** (name, email, school, graduation year or date and similar): filled from your profile every time and labelled **From profile**. If the profile has no value, the row links to **Profile** to add it; you can also answer it yourself. **First name** and **Last name** questions are the exception: your profile stores your full name only, and ResuApply never splits it, so you type the part asked for. Questions added before this rule follow it too.
- **Sensitive-factual** (work authorization, sponsorship): the profile value is shown but nothing is filled in until you press **Confirm this answer**. Until then the label is **User input required**. If the profile value changes later, you confirm again. The value comes from your authorization record for the country the question names: an explicit country name first, then the abbreviations **US**, **U.S.** or **USA** (an ordinary "Tell us …" never counts as the US). A question that names several countries, or a place ResuApply can't map to one country, stays unknown for you to answer. Only a question that names no country is answered from your single record, when you have exactly one.
- **Sensitive** (gender, race, disability, salary, consent and similar): you write the answer yourself, or choose **Skip this optional question** when it is optional. These are never drafted by the AI model and never saved to the answer bank.
- **Open-ended** (why do you want this role, describe a project): write an answer, or choose **Draft open answers with AI**. Drafts use the same approved, reduced context as tailoring, cite the profile items they are built from, and are checked against your profile and the question's length limit. A draft is labelled **AI draft (pending review)** and is not an answer until you press **Accept draft** (or **Discard draft**); accepting checks it again against your current profile. If you edit your profile so that it no longer supports an AI answer you accepted, **Draft open answers with AI** drafts that question again. The new draft appears as a **Replacement AI draft** below your accepted answer, which stays as it is: **Accept replacement** swaps it in (and clears the approval), **Discard replacement** keeps what you had, and a failed AI request changes nothing.
- **Unrecognized**: shown as **Confirm category** until you choose one with **Set category**. The app refuses changes that would let it answer a question it must not (for example, a sensitive question becoming factual). **Change category** and **Remove question** are under each row; removing a question never reuses its ID.

**Length limits.** A profile value longer than the question's limit is shown in full, labelled **Over the length limit**, with the count that exceeds it. ResuApply never shortens it: write a shorter answer yourself, or skip the question if it is optional. Until you do, it blocks approval, even on an optional question, and it can't be confirmed. A later profile edit that makes a value too long has the same effect. The counter and the server count lengths the way employer forms usually do, so an answer ResuApply accepts fits theirs: characters as a browser's length limit counts them (an emoji counts as 2, a line break as 1), and words split at spaces and at em and en dashes.

**Cited evidence.** An AI answer's numbers, credentials and technologies are checked against the sources it actually cites, including those entries' technology lists. Facts in an uncited summary or an unrelated entry do not support the answer. These checks run when drafting, accepting and approving. Optional questions can be explicitly skipped even after you accepted an answer, including a sensitive answer you decide to omit.

Labels you will see on a question: **From profile**, **AI draft**, **User answer**, **Missing information**, **User input required**, **Over the length limit**, **Skipped (optional)**, **Confirm category** and **AI draft (pending review)**.

**Answer bank.** After you accept an answer to a non-sensitive question, **Save to answer bank** keeps it. The **Answers** page lists everything saved, with the job it came from, and **Delete from answer bank** removes an entry. When a similar question appears on another job, **Start from a saved answer** shows the saved text; **Use as starting point** only fills the answer box, and you still press **Save my answer**. Values filled from your profile are not saved to the bank.

**Review and approve.** The **Review** section shows the package's state: **Draft** (not approved, or changed since), **Approved**, or **Stale** (your profile or the job changed after you approved it). It lists what blocks approval (no accepted resume, a resume that no longer matches the profile, an accepted AI-drafted answer that your edited profile no longer supports, a required question without an accepted answer, an unresolved sensitive or unrecognized question, a profile value over its length limit) and what is only worth a look (an optional question left unanswered, drafts waiting for review, no requirements reviewed). **Approve package** appears when nothing blocks it. Approval covers only the accepted resume and the questions entered in the workspace, not any other questions on the employer's form, and it never marks the job Applied. Editing the resume, a question or an answer clears the approval.

**Record what you submitted.** Submit the application yourself on the employer's site. Then, under **Tracking**, choose **Applied**; while the package is Approved, **Save the approved package as what I submitted** is ticked by default. Recording it saves a **submitted package**: the job details and description, the resume exactly as rendered, the answers and the profile and job revisions, listed under **Submitted packages** in the history. Open one to see it as submitted, including **Open the resume as submitted** for a print view of the frozen resume. Later changes to your profile or the job never alter it, though they mark the unsubmitted package Stale. Choose Applied with the box unticked, or without an approved package, to record a plain status change.

**Pages that are out of date.** **Confirm this answer**, **Accept draft**, **Approve package** and **Save the approved package as what I submitted** each apply to exactly what your page showed. Evidence confirmations also cover the source content you saw: if it changed, review it again before linking or reconfirming it. Job and requirement editors reject saves against an older revision. They keep your entered values, show the version saved now beside them, and save yours over it only when you tick **Save mine over it** (or **Save mine over them**). If it changed yet again, you compare again. Submitted-package review includes the saved job URL; changing only that URL requires a fresh review before recording a snapshot, without requiring a new resume. Overlapping question, answer, evidence, override, status and note saves preserve unrelated changes, and question and requirement IDs are never repeated.

## What's included

- **Profile:** a guided form for contact details, links, summary, education, experience, projects (with bullets and technologies), skills, skills you've confirmed you lack, certifications, preferences, availability and per-country work authorization (yes / no / unknown). Links must be web addresses, such as `github.com/you` or `https://example.com`; anything else (for example `javascript:` or `mailto:`) is refused. Every save goes through a **Review changes** step that lists each added, changed or removed fact. Entries, bullets and certifications get stable IDs (`exp-1`, `exp-1-b2`, `cert-1`) that survive edits, and deleted IDs are never reused. The revision increases only when something really changed. The validation and ID rules are ported from ResuSkill.
- **Resume import:** choose a PDF or Word (.docx) file, up to 10 MB, and it is read on this computer into the profile form, where you check every field before the usual review step. Nothing is sent to the AI model. With a saved profile, an import only adds what the profile lacks and never changes a saved fact. Scanned PDFs support opt-in local OCR; password-protected, damaged and unsupported files return a reason, and work authorization is never read from a resume.
  - **What it reads:** one- and two-column PDFs (a column is told apart by its section headings), dates and places set flush right on an entry's line, PDFs of several pages (page numbers and a header repeated on every page are left out), ligatures from LaTeX, Word list bullets, Word tables, text boxes and the first page's header and footer, the bullet marks Word and resume templates use, headings such as "Skills & Interests", "Research Experience" or "E X P E R I E N C E", and dates such as "Jan 2023 – Present", "2021–22", "Summer 2024" (the year only: a season names no month) or "May '24". Honours, the Dean's List, coursework and a minor are kept as bullets of their school.
  - **What it can't read (fill these in yourself, from the text shown beside the form):** a resume without recognizable section headings (only the contact details are read); text in images inside Word files, logos, icons or skill bars; a two-column PDF whose columns don't both have a section heading, or that a builder draws in an order other than column by column or line by line; headings in languages other than English; a heading that combines two profile sections, such as "Experience & Education" (Certifications with Skills or with Education is read line by line); entries laid out several to a table row; and Word headers and footers after the first section. OCR currently uses English and its recognition can be imperfect; extracted text stays visible for correction.
- **Jobs:** manual entry with title, company and pasted description (required), plus location and URL (optional). The description is stored exactly as pasted and is only ever shown as text. Changing the title, company, location or description increases the job revision. A link is only ever saved as a reference: saving a job never fetches it.
- **Greenhouse job import:** on **Add a job**, paste a posting link from `boards.greenhouse.io` or `job-boards.greenhouse.io` and choose **Import**. ResuApply fetches that posting and its application questions from Greenhouse's public job board (`boards-api.greenhouse.io`), fills in the title, company, location and description, and lists each question with its required flag, field type (short or long text, choose one, choose any) and category. Nothing is saved until you check it and choose **Save job**, which adds the questions you reviewed. Questions from the voluntary self-identification and demographic sections are always **Sensitive**; a field type ResuApply doesn't know is added as **Confirm category**; file uploads such as the resume are listed as left for the employer's form. Only https links on those two hosts are fetched, redirects are refused, an import gives up after 10 seconds or 2 MB, and a failed import changes nothing. The posting's text is information only, like a pasted description. Other job sites aren't fetched: paste the posting instead. An answer to an imported **Choose one** or **Choose any** question must name the form's choices as written there (one per line, or separated by commas or semicolons, for Choose any); until it does it shows **Not one of the form's choices** and blocks approval, even on an optional question. These questions are never drafted by the AI model.
- **Tracker:** every job with its review state (Draft, Approved or Stale, recalculated whenever the list is opened) and tracking status (Saved, Applied, Assessment, Interview, Rejected, Offer, Withdrawn), a dated status history and notes. Status dates are entered as `YYYY-MM-DD`, from 1990 up to today. Only you change the status; approval never sets Applied.
- **Job Workspace:** the job's details and description, tracking and notes, the requirements checklist and resume workflow, questions and answers, review and approval, and any submitted packages.
- **Requirements:** enter them yourself or have the AI model propose them; either way you review and correct each one before saving. Every requirement quotes words from the description, and if any excerpt can't be found there, nothing is saved. Each can have a comparable criterion (skills, degree, graduation window, location/work mode, work authorization, start date, years of experience). Requirement IDs (`r1`, `r2` …) stay stable across edits and are never reused.
- **Checklist:** Python compares each requirement with your profile and shows **Met**, **Unmet** or **Unknown** with the reason, grouped as sources, gaps and unknowns. A skill that isn't in your profile is Unknown unless you confirmed you lack it; anything that can't be compared stays Unknown until you link evidence. Gaps stay visible but never block an application. The checklist and each tracker card show how many required items are Met (for example **3 of 4 required items met**, or every item when none is marked required): a count with its denominator, not a score.
- **Evidence and overrides:** link profile bullets or entries as evidence yourself, or ask the AI model for suggestions and accept or reject each one. Suggestions change nothing until you accept them. You can override any status, and the override is always shown with your reason and the calculated status.
- **Resume:** manual or AI proposals, original/proposed comparisons with source IDs, claim checks, separate accepted content, stale-input checks, three saved layouts, browser Save as PDF and editable Word export. Regeneration and provider failures preserve accepted work.
- **Cover letter:** optional, written by you or drafted from cited profile facts and checked like AI answers; part of the approved package, frozen when you record Applied, and printable or downloadable as Word.
- **Questions and answers:** categories by fixed rules, profile-backed answers, your own answers, and AI drafts only for open-ended questions, each reviewed and accepted by you. Sensitive questions are never drafted or banked.
- **Answer bank:** the **Answers** page, with similar-question suggestions that only fill the answer box.
- **Review and approval:** Draft, Approved or Stale, listed blockers and warnings, and an explicit **Approve package** step that never sets Applied.
- **Submitted packages:** the approved package frozen when you record Applied, viewable (including its resume) after any later profile or job edit.
- **Greenhouse autofill (optional):** opens a standard Greenhouse-hosted form in a visible browser, fills verified contact fields, links, the attached resume PDF and approved non-sensitive answers, reads them back and leaves the rest, and the Submit button, to you.

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
                  form parsing, jobs, tracking, resume import, Greenhouse job import and autofill
  routes/         Profile, jobs, resume, questions, package/snapshot and answer bank pages
  templates/      Jinja2 pages
  static/         CSS and a small amount of JavaScript
scripts/          Live OpenRouter smoke test, local acceptance check and a fake-AI dev server
tests/            pytest: rules, mocked AI, HTTP integration and restart persistence
```

## Releases

| Release | Scope |
|---|---|
| **V1** | Profile, job entry, requirement checklist, resume tailoring, answers, approval, print-to-PDF, tracking |
| **V2** | PDF/DOCX profile import with optional OCR, resume layouts and DOCX export, Greenhouse job import, limited Greenhouse autofill (you still click Submit) |

## Stack

Python 3.12 · FastAPI · Jinja2 · SQLAlchemy · SQLite · Pydantic · HTTPX → OpenRouter (model configurable) and Greenhouse's public job board (job import, on request) · pypdf and python-docx for import/export · pypdfium2, Pillow and optional Tesseract for local OCR. Optional Playwright (pinned in `requirements-autofill.txt`) for Greenhouse autofill.

## License

[MIT](LICENSE)
