"""Install or update the app's packages in this environment, only when requirements.txt changed.

    .venv/bin/python -m scripts.ensure_packages

The start scripts (start.sh, start.bat) run it before every start. It records the hash of the
requirements file it installed in the environment (``.resuapply-requirements``), so an ordinary
start skips installing, and a ``git pull`` that changes the requirements updates them. It uses the
environment's pip, or uv when the environment was made without pip.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = Path(sys.prefix) / ".resuapply-requirements"


def digest(path: Path | None = None) -> str:
    return hashlib.sha256((path or REQUIREMENTS).read_bytes()).hexdigest()


def install_command(requirements: Path | None = None) -> list[str] | None:
    """How to install into this environment: its pip, or uv pointed at it; None without either."""
    requirements = requirements or REQUIREMENTS
    if importlib.util.find_spec("pip") is not None:
        return [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(requirements)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "-q", "--python", sys.executable, "-r", str(requirements)]
    return None


def main() -> int:
    wanted = digest()
    try:
        if STAMP.read_text().strip() == wanted:
            return 0
    except OSError:
        pass
    command = install_command()
    if command is None:
        print("ResuApply can't install its packages: this Python environment has no pip, and uv isn't installed. "
              "Delete the .venv folder and start again, or install uv (https://docs.astral.sh/uv/).")
        return 1
    print("Installing ResuApply's packages (first start, or the requirements changed)…")
    if subprocess.run(command, check=False).returncode != 0:
        print("Installing the packages failed (see above). Check your internet connection and start again.")
        return 1
    STAMP.write_text(wanted + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
