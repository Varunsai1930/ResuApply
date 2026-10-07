"""Application settings, read from environment variables and an optional .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ResuApply is a single-user local app. The bind address is fixed, not configurable.
HOST = "127.0.0.1"
# Host headers accepted by the app. Anything else is rejected (guards against DNS rebinding).
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESUAPPLY_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = Field(default=8000, ge=1, le=65535)
    data_dir: Path = Path("data")

    @property
    def resolved_data_dir(self) -> Path:
        path = self.data_dir.expanduser()
        return path if path.is_absolute() else PROJECT_ROOT / path

    @property
    def database_path(self) -> Path:
        return self.resolved_data_dir / "resuapply.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
