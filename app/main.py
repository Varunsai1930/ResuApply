"""FastAPI application factory.

Run with ``python -m app`` (binds to 127.0.0.1). ``create_app`` takes explicit settings so
tests can point it at a temporary database.
"""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import ALLOWED_HOSTS, Settings, get_settings
from .ai.client import OpenRouterClient
from .db import init_db, make_engine, make_session_factory
from .routes import answers, jobs, package, profile, questions, requirements, resume, sharing
from .input_cleaning import ControlCharacterMiddleware
from .security import HeadAsGetMiddleware, NoFramingMiddleware, SameOriginMiddleware
from .services.answers import upgrade_stored_questions
from .services.package import rewrite_approvals_with_stored_rules, upgrade_stored_approvals
from .templating import STATIC_DIR, templates


# Starlette's messages for a form over its field-count or field-size limits.
FORM_TOO_LARGE = re.compile(r"Too many (fields|files)|exceeded maximum size", re.IGNORECASE)


def create_app(settings: Settings | None = None, ai_transport: httpx.BaseTransport | None = None) -> FastAPI:
    """Build the app. ``ai_transport`` replaces the network for OpenRouter calls (tests use a mock)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.resolved_data_dir.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.database_url)
        init_db(engine)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        with app.state.session_factory() as session:
            upgrade_stored_questions(session)
            rewrite_approvals_with_stored_rules(session)
            upgrade_stored_approvals(session)
        key = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else None
        app.state.ai_client = OpenRouterClient(key, settings.openrouter_model, transport=ai_transport)
        try:
            yield
        finally:
            app.state.ai_client.close()
            engine.dispose()

    app = FastAPI(title="ResuApply", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(ControlCharacterMiddleware)  # innermost: routes only ever see cleaned forms
    app.add_middleware(HeadAsGetMiddleware)
    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(NoFramingMiddleware)  # added last, so it is outermost and covers every response
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(sharing.router)
    app.include_router(profile.router)
    app.include_router(jobs.router)
    app.include_router(requirements.router)
    app.include_router(resume.router)
    app.include_router(questions.router)
    app.include_router(package.router)
    app.include_router(answers.router)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def home():
        return RedirectResponse("/jobs", status_code=303)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        # Browsers ask for /favicon.ico even when a page links its icon; point them to the SVG.
        return RedirectResponse("/static/favicon.svg", status_code=301)

    def error_page(request: Request, status_code: int, detail: str):
        return templates.TemplateResponse(
            request, "error.html", {"status_code": status_code, "detail": detail, "active": None},
            status_code=status_code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if exc.status_code == 400 and isinstance(detail, str) and FORM_TOO_LARGE.search(detail):
            detail = ("The form was too large to read, so nothing was saved. "
                      "Go back, shorten or split what you entered, and try again.")
        return error_page(request, exc.status_code, detail)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # A malformed number in the address (/jobs/abc) names no page; anything else is a
        # damaged or incomplete form, which only a hand-made request can produce.
        if any(error.get("loc", ("",))[0] == "path" for error in exc.errors()):
            return error_page(request, 404, "There is no page at this address.")
        return error_page(request, 422, "The form arrived incomplete or damaged, so nothing was saved. "
                                        "Go back, reload the page and try again.")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        # Starlette still logs the exception; the user gets the app's page instead of plain text.
        return error_page(request, 500, "Something unexpected went wrong, and the action may not have been "
                                        "saved. Go back, reload the page and check before trying again.")

    return app


app = create_app()
