# Sequential optimization verification

Validated locally on macOS with Python 3.12 on 23 October 2026.

Workflow: implement one optimization, run focused checks and independent review, commit it, repeat the focused checks on the committed state, then proceed. Baseline `cd6635a` preserves the pre-existing workspace changes. Commits were kept local.

| # | Optimization | Commit | Focused verification |
|---|---|---|---|
| 1 | Autosave and recovery | 4ecf19b | 97 passed in 16.03s |
| 2 | Clear next actions | 72c8004 | 72 passed in 4.41s |
| 3 | Profile completeness guidance | 640977c | 35 passed |
| 4 | Resume quality checks | 61e81c0 | 19 passed |
| 5 | PDF readability checks | cb176be | 11 passed; 3 rendered layouts visually inspected |
| 6 | Clearer job-fit explanations | da69c71 | 107 passed |
| 7 | Resume version history | 29e381c | 45 passed |
| 8 | Examples by role and experience | d03a55f | 9 passed |
| 9 | Faster background PDF previews | e6bc883 | 18 passed; reviewer cache admission finding fixed and rechecked |
| 10 | Generation progress cancellation and recovery | ebd0b88 | 59 passed; independent review clean |
| 11 | Faster page loading | 5611e52 | 63 passed; independent review clean |
| 12 | Mobile layout improvements | b068a68 | 7 mobile/preview checks passed; independent review clean |
| 13 | Consistent visual hierarchy | ee43dea | 80 passed; desktop/mobile visually inspected; independent review clean |
| 14 | Keyboard navigation and accessibility | d3f72a4 | 15 passed; independent review clean |
| 15 | Application follow-up tracking | 5194043 | 56 passed; independent review clean |
| 16 | Local backup restore and data export | bc8d376 | 72 passed; desktop/mobile inspected; reviewer recovery-limit finding fixed and rechecked |
| 17 | Automated visual and performance checks | This commit | 9 visual, mobile and loading checks passed |

## Final combined validation

- Full suite: **1,886 passed** in 119.33 seconds; one existing Starlette/httpx deprecation warning.
- Real CLI smoke check: startup, occupied-port rejection, PDF/DOCX import, reviewed save, AI disabled, shutdown, restart persistence and Chromium PDF output passed using disposable fictional data.
- Every application JavaScript file passed `node --check`; `git diff --check` passed.
- Visual regression capture: 48 screenshots across 12 pages, light/dark modes and desktop/mobile widths. Representative desktop/mobile captures were also visually inspected.
- Highest measured HTML: 36,430 bytes; cold static assets: 367,313 bytes (the resume gallery); compressed CSS: 19,313 bytes; compressed JavaScript: 7,187 bytes.
- Automated bounds: HTML 192 KiB, static assets 512 KiB, CSS and JavaScript 32 KiB each. Local server render times are informational, not browser/network performance claims.

## Repeating the checks

```sh
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.smoke_local --output-dir validation-results/local-smoke --print-pdf validation-results/local-smoke/accepted-resume.pdf
RESUAPPLY_REQUIRE_UI_BROWSER=1 RESUAPPLY_UI_ARTIFACT_DIR=validation-results/ui .venv/bin/python -m pytest tests/test_ui_regressions.py -q
```

The Windows workflow includes a separate required-browser visual check and uploads its screenshots and JSON reports. This local run does not establish a new Windows CI result.

Portable backups are limited to 32 MB and the same database schema version; restore requires review and a restart. Visual checks cover layout geometry, heading contrast and image integrity; they do not replace a full assistive-technology audit.
