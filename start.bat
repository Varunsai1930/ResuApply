@echo off
rem Start ResuApply on Windows: double-click start.bat, or run it from a terminal.
rem The first start creates .venv (Python 3.12 through the py launcher, or uv), installs the packages and
rem creates .env from .env.example; later starts update the packages only when requirements.txt changed.
rem Then the app runs at http://127.0.0.1:8000 and opens in your browser. Stop it with Ctrl+C.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" >nul 2>nul
  if errorlevel 1 rmdir /s /q .venv
)
if exist ".venv\Scripts\python.exe" goto packages

py -3.12 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  py -3.12 -m venv .venv
  if errorlevel 1 goto novenv
  goto packages
)
where uv >nul 2>nul
if not errorlevel 1 (
  uv venv -q --python 3.12 .venv
  if errorlevel 1 goto novenv
  goto packages
)
echo ResuApply needs Python 3.12. Install it from https://www.python.org/downloads/windows/
echo (keep "py launcher" selected), or run: winget install Python.Python.3.12
echo Then run start.bat again.
pause
exit /b 1

:novenv
echo Making the .venv folder with Python 3.12 failed (see above). Delete the .venv folder and run
echo start.bat again; if it fails again, reinstall Python 3.12 from https://www.python.org/downloads/windows/
pause
exit /b 1

:packages
".venv\Scripts\python.exe" -m scripts.ensure_packages
if errorlevel 1 (
  pause
  exit /b 1
)
if not exist .env (
  copy /y .env.example .env >nul
  echo Created .env from .env.example. Add your OPENROUTER_API_KEY there to use the AI features.
)
".venv\Scripts\python.exe" -m app --open
