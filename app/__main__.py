"""Start the local server: ``python -m app``. Always binds to 127.0.0.1."""

import logging

import uvicorn

from .config import HOST, get_settings


def main() -> None:
    settings = get_settings()
    # App logs (AI request outcomes, never content or secrets) next to uvicorn's.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s %(message)s")
    ai = f"AI model {settings.openrouter_model} ({settings.openrouter_model_trust})" if settings.ai_configured else "AI off (no OPENROUTER_API_KEY)"
    print(ai)
    print(f"ResuApply running at http://{HOST}:{settings.port}  (data: {settings.database_path})")
    uvicorn.run("app.main:app", host=HOST, port=settings.port)


if __name__ == "__main__":
    main()
