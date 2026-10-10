# ResuApply — Implementation Plan

## 1. Goal and release boundaries

ResuApply is a local web app that turns a verified candidate profile and a pasted job description into an evidence checklist, a tailored resume, reviewed answers and an application record. It is the browser-based sibling of [ResuSkill](https://github.com/Varunsai1930/ResuSkill): the same workflow and rules, but with its own interface and a hosted model through OpenRouter.

**V1 delivers the complete manual workflow:**

```text
Create profile → Add job → Review requirements
→ Tailor resume → Draft answers → Review package
→ Save resume as PDF → Submit manually → Track outcome
```

**V2 adds PDF/DOCX profile import, Greenhouse job import and limited Greenhouse autofill.** The user always clicks the employer's Submit button.

Matching is checklist-based throughout. These stay out of both releases: numeric scores, automatic job discovery, other application platforms, email tracking, cloud accounts and DOCX export.

Success means the user can prepare and retrieve an accurate application package without re-entering their profile or losing reviewed work.

## 2. Core principle

The model drafts; Python decides. The model never writes to the canonical profile, never computes the checklist and never controls a browser. Every generated claim cites profile sources and passes deterministic checks before the user sees it as a proposal.

ResuApply uses the same rules as ResuSkill: requirement excerpt checks, checklist logic, claim validation, question categories and approval and staleness rules. The rules and their test scenarios are kept identical between the two projects. If ResuSkill's core is later published as a package, ResuApply may depend on it instead of keeping its own copy.

## 3. V1 implementation

### Foundation and interface

- Python 3.12, FastAPI, Jinja2, minimal JavaScript, synchronous SQLAlchemy sessions, SQLite and Pydantic.
- A virtual environment and pinned dependency files. One application; no queues or agent frameworks.
- Bind to `127.0.0.1`; one candidate and one local user.
- Three main views:
  - **Profile:** guided entry and confirmation of candidate facts.
  - **Jobs / Tracker:** saved jobs, review state, tracking status, dates and notes.
  - **Job Workspace:** description, requirement checklist, resume proposals, questions, review and export.
- AI buttons show progress and block duplicate requests. Saving profiles, jobs, notes and statuses works without an AI key.

### Profile and storage

Three tables:

| Table | Responsibility |
|---|---|
| `candidates` | Structured profile, stable source IDs, revision, timestamps |
| `jobs` | Company, title, location, URL, original description, requirements, evidence links, overrides, questions, revision |
| `applications` | Candidate/job references, current proposal, accepted package, answers, review state, submitted snapshots, tracking status, notes and dates |

An `answer_bank` table holds reusable approved answers to non-sensitive questions.

Nested education, experience, projects, requirements and package content are stored as validated JSON. Identifiers, relationships, statuses and timestamps use ordinary columns.

- The user can edit every profile field; the model cannot update the canonical profile.
- Every experience, project and education entry, and every bullet, has a stable ID that survives edits. Deleted IDs are never reused.
- Contact details, education, employment dates, skills, skills the user confirmed they lack, preferences, availability and links are stored explicitly.
- Work authorization and sponsorship are user-supplied, per-country facts. Missing values stay unknown.
- Profile and job revisions increase on relevant changes. Each generated package records the input revisions it was built from.

### Job entry and requirement checklist

A pasted description, a title and a company are required. Location and URL are optional; in V1 the URL is only a saved reference.

AI proposes structured requirements, and the user corrects them before evaluation. Each requirement records:

- its text and category;
- its importance: required, preferred or unspecified;
- a verbatim supporting excerpt from the description;
- an optional explicit criterion: skill list (all/any), degree level/field/status, graduation window, location/work mode, authorization and sponsorship, start date, or years of experience.

Every excerpt must appear in the description (after normalizing case, quotes and whitespace). If any excerpt fails, the whole extraction is returned for correction and nothing partial is saved.

The checklist is calculated in Python:

- **Met:** confirmed profile evidence satisfies the requirement.
- **Unmet:** confirmed profile evidence establishes a conflict.
- **Unknown:** information is missing, ambiguous or cannot be compared reliably.

Skill names are normalized with case folding and a small explicit alias map. A skill missing from the profile is **Unknown**, unless the user confirmed they lack it. Education, location, date and authorization conditions are compared only when the profile has the data. Experience and other uncomparable requirements become **Met** only through evidence links the user confirms: the model may suggest which bullets support a requirement, and the user accepts or rejects each one.

Sources, gaps and unknowns are shown separately. User overrides need a recorded explanation. Qualification gaps stay visible but never prevent the user from preparing an application.

### Hosted AI through OpenRouter

HTTPX calls OpenRouter's Chat Completions endpoint. Configuration:

```env
OPENROUTER_API_KEY=
OPENROUTER_MODEL=nvidia/nemotron-3.5-lightning:free
OPENROUTER_MODEL_TRUST=free        # free | trusted
```

- **The model is a setting.** Any OpenRouter model ID works. Free models may change availability or limits, so model replacement is an explicit configuration change. The app never switches models on its own.
- **Structured output:** each result is requested through one fixed schema-return tool and validated locally with Pydantic, because the default free endpoint does not enforce `response_format`. The tools return data only and cannot trigger actions.
- **Three AI operations:**

```python
parse_job(description) -> ParsedJob
tailor_resume(candidate_context, job_context) -> ResumeDraft
draft_answers(candidate_context, job_context, questions) -> AnswerDrafts
```

Plus an optional `suggest_evidence(candidate_context, requirements) -> EvidenceSuggestions`, whose results are only suggestions until the user confirms them.

- 90-second timeout per request. One corrective retry for malformed or invalid output; otherwise an error is shown.
- For authentication, quota, timeout or provider errors, existing work is kept and the user can retry manually.
- Validated results are reused when the task inputs, model and prompt revision are unchanged.
- The app records the model, prompt revision, input revisions, generation time and available usage metadata. Logs never contain secrets or candidate text.

### Outbound candidate context

Tailoring and answer operations receive a reduced candidate context, never the raw profile:

- Always removed: contact details, links, authorization, sponsorship, demographic information and legal declarations. These are filled back in locally from the profile when rendering.
- With `OPENROUTER_MODEL_TRUST=free` (the default, because the free endpoint asks users not to send personal or confidential data), the app shows an **editable preview** of the outbound career content before the first request. The user can remove confidential or identifying narrative. Approval is reused while the outbound context stays the same and requested again when it changes.
- With `trusted` (a provider and model whose data terms the user accepts), the preview is optional.
- Redaction is best effort and is described that way in the interface. Content the user removes stays local, and the user can draft those parts manually.

Placeholder substitution of employers, projects and dates is intentionally left out. It makes tailoring worse and makes restoring values fragile; choosing a trusted model is the cleaner answer when stronger privacy is needed.

### Resume tailoring

- One fixed, single-column HTML resume template with A4 print styles. Page breaks stay readable, and long profiles run to more pages instead of being clipped.
- The model selects, orders and proposes rewrites of existing bullets. Each proposed bullet cites source bullet IDs **from the same entry**.
- Names, employers, titles, dates, degrees, contact details and skill labels are rendered from trusted profile data.
- Deterministic rejection rules:
  - Unknown or cross-entry source references.
  - **Numbers and years:** numeric tokens (including number words such as "twelve" or "doubled") not present in the cited sources.
  - **Technologies:** terms from a built-in technology lexicon plus profile and requirement skills, with aliases, that appear in the bullet but not in the cited sources or the entry's technology list. Short or ambiguous terms (Go, R, C, React, Spring) match only with exact capitalization.
  - **Credentials:** certification, licence, patent, award, degree, publication and honour terms not present in the sources.
  - Skills not in the profile.
- Original and proposed text are shown side by side. Source references and automated checks support semantic review; they don't replace it.
- Proposals are kept separate from accepted content. Regenerating never overwrites the accepted resume; only an explicit accept does.
- The approved resume is printed through the browser's Save as PDF. Advanced layout and DOCX export remain deferred.

### Questions, review and tracking

Users paste the actual application questions and say whether each is required and whether it has a length limit (in characters or words).

Questions are categorized by explicit rules:

| Category | Handling |
|---|---|
| Factual (name, email, phone, links, graduation, start date, school, degree, major, GPA, location) | Filled from the profile; first and last names are typed by the user, because the profile stores only the full name and it is never split |
| Sensitive-factual (work authorization, sponsorship) | Filled from the profile, then the user must explicitly confirm it |
| Sensitive (demographics, disability, veteran status, criminal history, salary, legal attestations, consent) | The user answers directly or explicitly skips an optional question |
| Open-ended | Source-backed AI draft, checked with the same claim rules plus the length limit |
| Unrecognized | The user confirms the category before anything is generated |

Answer labels: **From profile**, **AI draft**, **User answer**, **Missing information**, **User input required**. Approved non-sensitive answers can be saved to the answer bank and offered as a starting point for similar questions.

Factual field requests are distinguished from narrative questions that mention those fields. AI answer validation uses only cited source text and those sources' technology lists; an uncited summary or unrelated skill cannot justify a claim. Optional accepted answers can still be explicitly skipped. Browser counters and server validation count characters as Unicode code points.

Preparation and tracking are kept separate:

- **Review state:** Draft, Approved or Stale.
- **Tracking status:** Saved, Applied, Assessment, Interview, Rejected, Offer or Withdrawn.

Approval requires:

- an accepted resume that still validates against the current profile;
- an accepted answer for every required question;
- every sensitive question resolved (answered, confirmed or explicitly skipped);
- no profile value over its question's length limit, even on an optional question (values are never shortened: the user writes a shorter answer or skips an optional question).

It does not claim that employer questions not entered in the workspace are complete.

Editing package content clears approval. Changing the profile or job marks unsubmitted packages Stale. Approval never marks an application Applied.

Confirming a profile value, accepting an AI draft, approving and recording a submitted package apply only to what the user's page showed. If the stored content changed since, the action is refused and the current content is shown for review.

Evidence linking and reconfirmation likewise bind to the displayed sources. Job and requirement edits reject stale revisions; related read-modify-write operations lock and reload stored rows so overlapping saves preserve other evidence, overrides, status events and notes. Evidence suggestion caches remain associated with the job displaying them.

When recording Applied, the user confirms which approved package they used; it is saved as a submitted snapshot, with its rendered resume and the profile revision it was built from. Later profile changes never alter it.

Snapshot review covers every frozen job field, including the posting URL. A URL-only edit invalidates the old snapshot review token without requiring resume regeneration.

## 4. V2 additions

### PDF and DOCX profile import

- Extract text locally with `pypdf` (PDF) and `python-docx` (DOCX paragraphs and tables).
- Support text-based PDFs. Scanned or image-only PDFs need manual entry; OCR is deferred.
- Show the extracted text and a proposed structured profile before saving.
- Extract contact details and identifying values locally. Any AI-assisted organization of career content uses the same outbound-context rules as V1.
- Imported facts need user confirmation. The current profile is never overwritten automatically; a field-level diff is shown first.
- Existing source IDs are kept for unchanged entries, and the revision increases when confirmed changes are saved.
- Unreadable, encrypted or unsupported files return clear errors without touching existing data.

### Greenhouse job import

- For a `boards.greenhouse.io` or `job-boards.greenhouse.io` URL, fetch the posting and its application questions from Greenhouse's public Job Board API (`/v1/boards/{board}/jobs/{id}?questions=true`).
- Save the description as the job's original text and pre-fill the question list (required flags and field types). The user reviews both.
- Other URLs remain saved references.

### Greenhouse autofill

Supports standard Greenhouse-hosted application forms only. Embedded employer implementations and other platforms are unsupported. Because Greenhouse allows custom questions, support is defined by verified fields, not a promise to handle every form.

- A visible Playwright browser is launched only by an explicit user action.
- Requires an approved, current application package.
- The user attaches the PDF saved from that package, and the app associates it with the package.
- The form is inspected before filling, using explicit label/role mappings.
- Fills verified contact fields, supported links, the resume attachment and approved non-sensitive text answers.
- Sensitive questions, consent controls, unsupported widgets and ambiguous fields are left for the user.
- Existing field values are kept unless the user explicitly asks to replace them.
- Filled values are read back, and mismatches or skipped fields are shown.
- The browser stays open for review and manual submission. The app never clicks Submit or sends keys that could submit.
- Control returns to the user on CAPTCHA, login, unexpected redirects or a changed form structure.
- Applied is recorded only after the user confirms. Browser resources are cleaned up on explicit close or app shutdown.

The model has no browser-control role. Autofill uses reviewed data and deterministic mappings.

## 5. Delivery order and acceptance tests

| Milestone | Deliverable | Acceptance gate |
|---|---|---|
| **1 — Foundation** | Profile with stable IDs, manual job entry, tracker, persistence | Save data, restart and reopen it; IDs survive edits |
| **2 — Assessment** | OpenRouter client, requirement extraction and review, deterministic checklist, evidence suggestions and links, overrides | Every result shows its evidence, and unknowns stay Unknown |
| **3a — Tailoring** | Outbound-context preview, resume proposals, claim validation, side-by-side review, print export | A Python-only source never gains Java, new metrics or dates; the accepted resume prints cleanly |
| **3b — Complete V1** | Questions, answers, answer bank, approval, staleness, submitted snapshots | Prepare, approve, submit manually and retrieve an unchanged package after profile edits |
| **4 — V2 import** | PDF/DOCX extraction with profile diff; Greenhouse job import | Import without silently changing existing facts |
| **5 — V2 autofill** | Limited Greenhouse adapter and browser handoff | Verify supported fields with zero automated submissions |

pytest covers matching, validation, revision handling and FastAPI integration. Provider responses are mocked, so ordinary tests need no API key. Before the AI integration is called complete, a live smoke test runs with synthetic, non-identifying content.

Required scenarios:

- A Python-only source does not gain Java, invented achievements or new metrics.
- Employers, dates, degrees and authorization remain unchanged.
- Missing information stays Unknown; explicit conflicts stay visible.
- Job text cannot instruct the system to change profile facts or perform actions.
- Outbound candidate requests exclude contact details, sensitive fields and anything the user removed in the preview.
- Invalid source references, malformed responses, quota errors and timeouts keep existing drafts intact.
- Regenerating keeps accepted content; changed inputs invalidate approval.
- Sensitive answers never enter the answer bank.
- Submitted snapshots stay unchanged after later edits.
- Print output is readable for both short and long profiles.
- PDF/DOCX imports require confirmation; failed imports leave the profile intact.
- Greenhouse fixtures cover existing answers, custom questions, unsupported controls, upload verification and field readback.
- Autofill tests capture submission events and assert that automation causes none. A manual smoke check confirms behavior on a supported hosted form without submitting.

## 6. Documentation and defaults

- The README and this plan stay consistent: one V1 definition, the milestones above, checklist examples, the outbound-context rules and manual submission ownership.
- No numeric scores, no `score_job()`, no Playwright setup before V2, no ambiguous "Approve Submission" controls.
- Document the actual tested setup commands, OpenRouter configuration, model limitations and the V1/V2 support boundary.
- Version control ignores the database, uploaded resumes, generated files, secrets and browser state.
- Start with a fresh database; no migration of existing application data is needed. V1 data is preserved when V2 is introduced.
- Defaults: one active profile, localhost access, one fixed resume template, manual tracking and explicit user review.
- Free-model availability is outside the app's control. If the configured endpoint becomes unavailable, the manual workflow keeps working and the failure is reported; replacing the model is an explicit configuration change.
