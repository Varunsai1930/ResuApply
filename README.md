# ResuApply

A local job application copilot web app. It turns your verified profile and a pasted job description into an evidence checklist, a tailored resume, reviewed answers and an application record, **without inventing anything**.

> **Status:** planned. See [PLAN.md](PLAN.md) for the full design and milestones. The same workflow is already available as an agent skill for Claude Code and Codex: [ResuSkill](https://github.com/Varunsai1930/ResuSkill).

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

## Releases

| Release | Scope |
|---|---|
| **V1** | Profile, job entry, requirement checklist, resume tailoring, answers, approval, print-to-PDF, tracking |
| **V2** | PDF/DOCX profile import, Greenhouse job import, limited Greenhouse autofill (you still click Submit) |

## Planned stack

Python 3.12 · FastAPI · Jinja2 · SQLAlchemy · SQLite · Pydantic · HTTPX → OpenRouter (model configurable). Playwright only in V2.

## License

[MIT](LICENSE)
