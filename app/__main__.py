"""Start the local server: ``python -m app``. Always binds to 127.0.0.1."""

import uvicorn

from .config import HOST, get_settings


def main() -> None:
    settings = get_settings()
    print(f"ResuApply running at http://{HOST}:{settings.port}  (data: {settings.database_path})")
    uvicorn.run("app.main:app", host=HOST, port=settings.port)


if __name__ == "__main__":
    main()
