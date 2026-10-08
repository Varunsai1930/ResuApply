"""Run the app with a local stand-in for OpenRouter, for trying the AI screens without a key.

    .venv/bin/python -m scripts.fake_ai_server

Nothing leaves your machine. The stand-in is deliberately simple and not a model:
- parse_job turns each "- " bullet line of the description into a requirement
  (excerpt = the line, no criterion);
- suggest_evidence suggests bullets that share a longer word with the requirement.
- tailor_resume selects the shared entries and keeps their original bullet text.
- return_answers drafts each open question by quoting the first shared bullet that fits its limit.
Use a separate data folder (RESUAPPLY_DATA_DIR) if you don't want test data in data/.
"""

from __future__ import annotations

import json
import re

import httpx
import uvicorn
from pydantic import SecretStr

from app.config import HOST, get_settings
from app.main import create_app

STOP = {"experience", "with", "and", "the", "for", "using", "years", "strong", "ability", "knowledge"}


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z+#.]{3,}", text.lower()) if w not in STOP}


def _parse_job(user: str) -> dict:
    posting = json.loads(user.split("<job_posting>", 1)[1].split("</job_posting>", 1)[0])
    reqs = []
    for line in posting["description"].splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ", "• ")) and len(line) > 4:
            text = line[2:].strip()
            reqs.append({"text": text.rstrip("."), "category": "other", "importance": "unspecified",
                         "excerpt": text, "criterion": None})
    return {"requirements": reqs}


def _suggest(user: str) -> dict:
    reqs = json.loads(user.split("<requirements>", 1)[1].split("</requirements>", 1)[0])
    context = json.loads(user.split("<candidate_content>", 1)[1].split("</candidate_content>", 1)[0])
    bullets = [b for s in ("experience", "projects", "education") for e in context[s] for b in e["bullets"]]
    out = []
    for req in reqs:
        hits = [b["id"] for b in bullets if _words(b["text"]) & _words(req["text"])]
        out.append({"requirement_id": req["id"], "source_ids": hits[:3], "reason": "Shares key words (fake model)"})
    return {"suggestions": out}


def _tailor(user: str) -> dict:
    context = json.loads(user.split("<candidate_content>", 1)[1].split("</candidate_content>", 1)[0])
    return {
        "summary": {"text": context["summary"]["text"], "sources": ["summary"]} if context.get("summary") else None,
        "experience": [{"entry": e["id"], "bullets": [{"text": b["text"], "sources": [b["id"]]}
                         for b in e["bullets"]]} for e in context["experience"]],
        "projects": [{"entry": e["id"], "bullets": [{"text": b["text"], "sources": [b["id"]]}
                       for b in e["bullets"]]} for e in context["projects"]],
        "education": [e["id"] for e in context["education"]],
        "certifications": [c["id"] for c in context["certifications"]],
        "skills": list(context["skills"]),
    }


def _answer(user: str) -> dict:
    questions = json.loads(user.split("<application_questions>", 1)[1].split("</application_questions>", 1)[0])
    context = json.loads(user.split("<candidate_content>", 1)[1].split("</candidate_content>", 1)[0])
    bullets = [b for s in ("experience", "projects", "education") for e in context[s] for b in e["bullets"]]
    summary = [context["summary"]] if context.get("summary") else []

    def fits(text: str, question: dict) -> bool:
        size = len(text.split()) if question["unit"] == "words" else len(text)
        return not question["limit"] or size <= question["limit"]

    answers = []
    for question in questions:
        quote = next((b for b in [*bullets, *summary] if fits(b["text"], question)), None)
        if quote:
            answers.append({"question_id": question["id"], "text": quote["text"], "sources": [quote["id"]]})
    return {"answers": answers}


def _handle(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    tool = body["tools"][0]["function"]["name"]
    user = body["messages"][1]["content"]
    handlers = {"return_requirements": _parse_job, "return_evidence_suggestions": _suggest, "return_resume": _tailor,
                "return_answers": _answer}
    args = handlers[tool](user)
    return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [
        {"id": "fake", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}]}}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})


def main() -> None:
    settings = get_settings().model_copy(update={"openrouter_api_key": SecretStr("fake-local-key"), "openrouter_model": "fake/local-stand-in"})
    print(f"ResuApply with a FAKE model at http://{HOST}:{settings.port}  (data: {settings.database_path})")
    uvicorn.run(create_app(settings, ai_transport=httpx.MockTransport(_handle)), host=HOST, port=settings.port)


if __name__ == "__main__":
    main()
