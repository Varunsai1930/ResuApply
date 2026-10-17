# ResuApply implementation and failure audit

Reviewed on 14 October 2026 against the current working tree, including its existing uncommitted UI and test changes. Application source and existing tests were not changed during this audit.

Implementation follow-up: recommendations 1–4 have now been implemented. New invalid Unicode is refused, legacy profile text can be corrected through review and save, damaged DOCX files return import errors, editable forms guard unsaved work, textarea values preserve initial newlines, AI exchanges have a cancellable total deadline and bounded responses, malformed numeric inputs return validation errors, and the reminder test uses a consistent date. Provider and Windows acceptance harnesses are prepared; their live/native validation remains pending by the user's choice. The original findings and reproductions below describe the audited version before those fixes. Greenhouse autofill remains unstarted.

## Implementation remaining

Updated 17 October 2026. Everything in PLAN.md is implemented, plus cover letters, Lever and Ashby autofill, Alembic migrations, background AI requests and one-command start scripts. What remains is validation on real services and two decisions.

| Work | Status | Evidence |
| --- | --- | --- |
| Live OpenRouter validation | Done with the configured free model (`nvidia/nemotron-3.5-super-vl:free`): requirement extraction, evidence suggestions, resume tailoring, answer drafts and cover letters each passed local validation at least once. The free tier also timed out at 90 s or returned provider errors on several requests, and allows 50 free-model requests a day. One false positive was fixed (a cited entry's own dates now count as support) and the cover letter prompt was tightened. | `scripts/smoke_openrouter.py` (five stages) |
| Autofill on a live form | Fixture forms for Greenhouse, Lever and Ashby pass end to end. The form reader and plan were run read-only on live Lever and Ashby application pages; no live form has been filled. | `tests/test_autofill.py`, `tests/test_postings.py` |
| Windows validation | Windows CI (`windows-validation.yml`) passes; the earlier hang was a 10 MB test parameter id. | GitHub Actions, PR #3 |
| ResuSkill rules | Claim checks now agree with ResuSkill (held/in-progress credentials and CFA levels were ported). ResuSkill lacks nine form-field labels ResuApply recognises; `scripts/rules_parity.py` lists them. A shared package would need ResuSkill restructured, which is the owner's decision. | `scripts/rules_parity.py` |
| Workday autofill | Not planned: its sign-in and multi-step forms fall outside what deterministic autofill can verify. | PLAN.md |
| Desktop packaging | `start.sh` and `start.bat` set up and start the app in one command; a bundled desktop app (with Tesseract and a browser) is not built. | README.md, Quick start |

## Original confirmed findings, in recommended fix order

### 1. P2 — Invalid Unicode can be saved and leave the application pages unusable

The profile payload shape check accepts every Python string, including unpaired Unicode surrogates. Profile normalization preserves them and the save commits them. Rendering the saved value as UTF-8 then raises `UnicodeEncodeError`.

Evidence: [profile_form.py](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/services/profile_form.py:146), [profile normalization](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/services/profile.py:255), [persistence](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/services/profile.py:653), [save route](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/routes/profile.py:233).

Reproduced in a temporary database by posting this JSON through the reviewed-profile `payload` field, with `base_revision=0`:

```json
{"contact":{"name":"A\ud800","links":{}},"summary":"","preferences":{},"availability":{}}
```

Saving returned HTTP 303. Subsequent requests to `/`, `/jobs`, `/profile` and `/profile/edit` returned HTTP 500. After closing and recreating the app against that same temporary database, the profile and editor still returned 500. The exception was `UnicodeEncodeError: surrogates not allowed`.

This requires a corrupted or hand-crafted payload; ordinary browser text input did not reproduce it. Its impact is persistent because the editor needed to repair the profile also fails. Reject invalid Unicode before persistence and provide a safe recovery path for already stored invalid values. The demonstrated failures are page/request failures, not termination of the Python process.

### 2. P2 — Some damaged DOCX uploads escape import error handling

[resume_import.py](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/services/resume_import.py:193) catches several parser errors but omits `AttributeError` and `RuntimeError`. [The import route](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/routes/profile.py:293) handles only `ResumeImportError` from extraction.

Reproduced by generating a small DOCX and replacing `word/document.xml` with a well-formed Word document root without a body. Extraction raised `AttributeError` at the block traversal. A wrong XML root and a ZIP member marked encrypted also reproduced unhandled parser exceptions. Each upload to `/profile/import` returned HTTP 500 rather than the expected readable import error.

Normalize these parser failures to `ResumeImportError`, and add regression cases. No profile changes were saved by these failed imports.

### 3. P2 — Navigation silently discards unsaved answers and notes

[forms.js](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/static/forms.js:43) records dirty inputs only inside `form[data-guard]`. [Manual answer forms](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/templates/questions_section.html:33), the add-question form and [notes form](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/templates/tracking_section.html:109) omit this attribute.

Type an answer, then change workspace step or submit another question's answer. The page navigates away and loses the first answer without an unsaved-changes warning. A synthetic Chromium reproduction showed `beforeunload` was not canceled for the current answer form; a guarded control form did cancel it.

Apply the existing guard to editable forms consistently. Saving one form should continue to leave other edited forms guarded; the current JavaScript already removes only the submitted form from its dirty set.

### 4. P2 — The AI timeout does not enforce a total request deadline

[OpenRouterClient](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/ai/client.py:80) sets HTTPX's phase timeouts, then [blocks on the complete response](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/ai/client.py:114). There is no total elapsed-time cutoff. A provider that keeps sending small chunks before each read timeout can keep the request and its operation lock active beyond the promised 90-second limit.

Verified with the installed HTTPX/httpcore implementation and a local socket-pair response through the real HTTP transport: a 0.1-second timeout still allowed a response taking approximately 0.325 seconds when chunks arrived every 0.03 seconds. This was a shortened analogue of the configured 90-second timeout, with no external provider request.

Add a total deadline for each provider exchange and bound response size. Verify that timeout handling releases the operation lock, preserves accepted work and allows retry. This demonstrates a prolonged-request risk, not a measured live OpenRouter outage.

### 5. P3 — Opening and saving an unchanged job can change its description

[job_form.html](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/templates/job_form.html:109) puts the saved description immediately after the opening `<textarea>` tag. HTML removes a leading line feed there. The [job input service](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/services/jobs.py:57) intentionally preserves leading newlines.

End-to-end Chromium/TestClient reproduction: save a job whose description starts with `\n`, open Edit details, submit the browser's unchanged field values. The leading newline disappears, the response says `job_updated`, and revision increases from 1 to 2. Existing preparation based on the previous revision becomes stale.

Preserve the first newline when rendering textarea values, and verify that an unchanged editor submission leaves both content and revision unchanged.

### 6. P3 — Oversized numeric values produce 500s instead of validation errors

Two related malformed-input paths were reproduced:

- [tracking.parse_days](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/services/tracking.py:51) calls `int()` on arbitrary digit strings without catching Python's integer-conversion-limit `ValueError`. A 5,000-digit `follow_up` on `/jobs/{id}/status`, or `days` on `/jobs/{id}/follow-up/remind`, returns HTTP 500.
- [profile._load_payload](/Users/varun/Downloads/Varun/ResuMax/ResuApply/app/routes/profile.py:145) catches `JSONDecodeError` and `RecursionError` but omits the `ValueError` raised for an excessively large JSON integer. A `payload` containing a 5,000-digit integer returns 500 on `/profile/save` and `/profile/edit`.

These are hand-crafted request cases rather than ordinary form submissions. Compare reminder values against the allowed choice strings before conversion, and normalize JSON numeric-limit errors to the existing damaged-payload response. These reproductions did not persist changed data.

## Test results and test maintenance

The full existing suite returned **1,387 passed, 2 failed** in approximately 34 seconds:

- `test_startup_problems_end_with_a_plain_message` failed because the sandbox blocked `socket.bind`. Re-running that exact test with localhost socket access passed. This was an environment restriction.
- `test_the_board_shows_due_follow_ups_in_a_banner_and_on_the_card` failed because [its imported board helper](/Users/varun/Downloads/Varun/ResuMax/ResuApply/tests/test_jobs_board.py:18) uses 11 October 2026, while [the follow-up test](/Users/varun/Downloads/Varun/ResuMax/ResuApply/tests/test_follow_up.py:20) uses the actual current date. Aligning those clocks in memory made the test pass. Use one injected/frozen date; the live board route already uses the current date correctly.

The listed new findings were checked separately with temporary databases, synthetic files and browser/HTTP reproductions. Passing existing tests did not cover these cases. No live OpenRouter calls or real employer forms were used, and Windows was not tested.

Codebase Memory was used first to identify relevant modules, symbols and call paths. Coverage was checked for relied-on paths; relevant partially indexed templates were read directly. Findings were verified against current source, not inferred from graph absence alone.

## Recommended next work

1. **Implemented:** Prevent and recover from invalid stored Unicode; handle damaged DOCX files.
2. **Implemented:** Guard unsaved workspace edits and preserve textarea values exactly.
3. **Implemented:** Enforce the AI request deadline and normalize malformed numeric inputs.
4. **Implemented:** Fix the test clock mismatch, then rerun the suite and the new regressions.
5. **Prepared; execution pending:** Complete live provider and Windows acceptance checks. Four-operation provider smoke, local CLI acceptance, Windows commands and a Windows CI workflow are ready. Live provider and native Windows runs remain pending as requested.
6. Implement Milestone 5 Greenhouse autofill and its no-submission tests.

## Follow-up implementation validation

At completion of the first three recommendations, the full regression suite returned **1,446 passing tests and one pre-existing failure**. That remaining failure was the board/follow-up test clock mismatch described above; recommendation 4 subsequently corrected it, as recorded below. The startup socket test passed with the required localhost test access. After increasing the process tests' startup allowance for slower hosts, the AI-focused suites also passed all **190 tests**.

New regressions cover strict Unicode rejection at the request, service and database boundaries; legacy-profile recovery across restart without changing raw stored text until a reviewed save; stable IDs and revisions; approval/sharing blocking during recovery; damaged DOCX imports leaving data unchanged; numeric input validation; exact description roundtrips; and unsaved form tracking. Actual Chromium checks confirmed native leave warnings, canceled-navigation recovery, submitting one form while retaining other edits, and unchanged job content/revision/approval.

AI tests cover slow response bodies, header/connect stalls, stalled system DNS, response size limits, subprocess startup/crash/interruption, saved-cache preservation and retries. The real network path uses a short-lived request process so the deadline can stop the OS resolver too. It closes and joins the worker before returning, and removes its private bounded-response temporary file. Each real request uses fresh connections. Tests used synthetic responses and offline socketpairs, and confirmed no surviving request workers or temporary response directories. Live provider and Windows validation remain pending.

Existing uncommitted UI changes were preserved. The fixes and independent reviews were verified against current source using the Codebase Memory discovery and coverage workflow.

## Recommendations 4 and 5 follow-up validation

At completion of recommendations 4 and 5 preparation, the full suite passed: **1,463 passed, one dependency deprecation warning, no failures** in 44 seconds. The board and follow-up tests share the injected clock; regression dates include the current date, past and future dates, and a leap day.

The OpenRouter smoke now exercises all four operations: job parsing, evidence suggestions, resume tailoring and answer drafting. It also accepts the proposals, approves a package and records a synthetic submission snapshot without changing the source profile. Offline regressions check failure handling, key redaction, data isolation and cleanup. A real provider run remains pending at the user's request.

The portable local acceptance script passes on macOS using the real application launcher, synthetic PDF/DOCX imports, reviewed saves, accepted resume print HTML, occupied-port rejection, shutdown and restart persistence. Its temporary data path contains spaces and Unicode, and it disables AI even when a key is inherited. A regression proves that an unrelated healthy server cannot satisfy startup readiness or receive the script's requests. Missing optional browser dependencies produce a plain error and remove temporary data.

Windows command examples and a Windows/Python 3.12 GitHub Actions workflow were prepared, including optional Chromium PDF checks and saved synthetic results. The workflow YAML parsed successfully, and the runtime, test and optional browser dependency set resolved to **37 Windows/Python 3.12 compatible wheels** in a disposable download directory. These preparation checks do not establish native Windows acceptance. At that point the Windows workflow had not been pushed or run, native Windows execution remained pending, and the optional browser-PDF path had not been executed in that follow-up.

## Review fixes and push validation

All four findings from the follow-up review are fixed in separate commits:

- Historical submission snapshots recover unreadable profile text for display without rewriting the original frozen profile. Restart, stored approval upgrades, snapshot appends, and strict validation of new and mutated snapshots are covered.
- AI submissions ask about other unsaved edits before entering their busy state. Cancellation leaves controls usable; confirmed requests prevent duplicates; history restoration restores labels, accessibility state and originally disabled controls.
- Local acceptance database connections close on successful reads and query failures before temporary cleanup.
- The stalled-DNS test permits Windows asyncio's loopback socketpair, with a regression that exercises that Windows fallback on macOS too.

The full suite passes **1,476 tests**, with one dependency deprecation warning and no failures. Chromium confirmed that canceling the AI action sends no request, preserves the answer and leaves controls usable; confirming sends exactly one delayed request and keeps AI controls locked. A subsequent browser check also covers a later submit listener canceling the AI action: cleanup runs after full event dispatch and restores controls without sending a request. An independent review found no additional defects in the historical snapshot fix. Live OpenRouter validation remains deferred.

The [first native Windows CI run](https://github.com/Varunsai1930/ResuApply/actions/runs/38495593924) installed Python, application dependencies and Chromium successfully, but exceeded its 20-minute limit during the full test suite and was canceled before local acceptance/PDF checks. GitHub supplied no archived test logs for that run. Native Windows acceptance remains pending; subsequent pushes trigger a fresh workflow run.

## OCR, resume layouts and Word export validation

The requested additions are implemented: opt-in local English OCR for scanned PDF pages, saved Classic/Modern/Compact layouts and editable DOCX export for current accepted resumes and frozen submitted packages. OCR remains part of the reviewed profile-import workflow and never saves facts automatically. Layout changes require a fresh package token and clear approval only when the choice changes. Old application rows and snapshots use Classic; frozen exports remain independent of later profile and layout edits.

The explicitly requested review-agent workflow found two introduced defects, both fixed before completion:

- **P2 — Hybrid PDF body omitted:** a scanned body beneath a selectable contact header could exceed the text-length cutoff and bypass OCR. Image detection now measures painted image coverage independently of text length, including Form transforms and rectangular clipping. Direct and Form-wrapped scanned bodies recognize successfully with actual Tesseract. Small logos, headshots, clipped images and repeated logos bypass OCR; selectable headers are retained.
- **P2 — Malformed Word attachment filename:** Unicode normalization could turn a fullwidth quotation mark into an unescaped ASCII quote after the filename had been sanitized. The normalized ASCII fallback is sanitized again. Regression cases and Chromium download checks confirm a valid filename and separate UTF-8 filename parameter.

Visual inspection also found an inherited blue border in the default Word Title style. Both the style border and any direct name-paragraph border are removed, with regression coverage for injected template borders in every layout.

Final validation: **1,584 tests passed**, with one existing dependency deprecation warning and no failures. Independent follow-up reviews reported no additional qualifying defects after the fixes. OCR service and route checks include actual local Tesseract, missing-engine errors, deadline cleanup, renderer failures, image limits, mixed pages, opt-in behavior and preserved saved profiles. Export tests cover selected content, XML validation, layout persistence, migration, stale-token rejection, approval invalidation and frozen snapshots.

Six fictional Word resumes and six browser PDFs (short and multi-page samples in every layout) were rendered and every page inspected. Both formats produced one-page short samples and two-page long samples with readable wrapping. Chromium checks confirmed identical selected content across layouts, no desktop/mobile horizontal overflow and retention of every selected bullet in A4 PDF output. Word rendering used the bundled document runtime. Native Windows acceptance and live OpenRouter validation remain pending as previously requested.
