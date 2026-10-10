"""Start the local server: ``python -m app``. Always binds to 127.0.0.1.

Before starting, it checks the settings, the data folder and the port, so a problem ends with
a plain message instead of a traceback (and "running at" is only printed once they pass).
"""

import logging
import socket
import sys

import uvicorn
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from .config import HOST, Settings, get_settings
from .db import init_db, make_engine

ENV_NAMES = {
    "port": "RESUAPPLY_PORT",
    "data_dir": "RESUAPPLY_DATA_DIR",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "openrouter_model": "OPENROUTER_MODEL",
    "openrouter_model_trust": "OPENROUTER_MODEL_TRUST",
}


def load_settings() -> Settings:
    try:
        return get_settings()
    except ValidationError as exc:
        problems = [f"  {ENV_NAMES.get(str(e['loc'][0]), e['loc'][0])}: {e['msg']}" for e in exc.errors()]
        sys.exit("ResuApply can't start: a setting in .env or the environment is invalid.\n"
                 + "\n".join(problems) + "\nFix it (see Configuration in README.md) and start again.")


def check_storage(settings: Settings) -> None:
    """Create the data folder and open (or create) the database, as startup will."""
    folder = settings.resolved_data_dir
    try:
        folder.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.database_url)
        try:
            init_db(engine)
        finally:
            engine.dispose()
    except (OSError, SQLAlchemyError) as exc:
        reason = getattr(exc, "orig", None) or exc
        sys.exit(f"ResuApply can't use its data folder {folder}: {reason}\n"
                 "Check RESUAPPLY_DATA_DIR and the folder's permissions. If "
                 f"{settings.database_path.name} there is damaged or isn't a ResuApply database, "
                 "move it elsewhere to start with an empty one.")


def check_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as uvicorn does
        try:
            probe.bind((HOST, port))
        except OSError:
            sys.exit(f"Port {port} is already in use on {HOST}. Stop the program using it, or choose "
                     "another port with RESUAPPLY_PORT (for example RESUAPPLY_PORT=8001).")


def main() -> None:
    settings = load_settings()
    check_storage(settings)
    check_port(settings.port)
    # App logs (AI request outcomes, never content or secrets) next to uvicorn's.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s %(message)s")
    ai = f"AI model {settings.openrouter_model} ({settings.openrouter_model_trust})" if settings.ai_configured else "AI off (no OPENROUTER_API_KEY)"
    print(ai)
    print(f"ResuApply running at http://{HOST}:{settings.port}  (data: {settings.database_path})")
    uvicorn.run("app.main:app", host=HOST, port=settings.port)


if __name__ == "__main__":
    main()
