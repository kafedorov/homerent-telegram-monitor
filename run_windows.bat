@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" goto dependencies
where py >nul 2>nul
if errorlevel 1 goto usepython
py -m venv .venv
goto checkvenv
:usepython
python -m venv .venv
:checkvenv
if errorlevel 1 goto failed
:dependencies
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import playwright.sync_api" >nul 2>nul
if not errorlevel 1 goto browser
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
:browser
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
echo Starting Cian Telegram monitor. Keep this window and Chromium open.
echo First run: create a bot at https://t.me/BotFather and enter its token locally.
".venv\Scripts\python.exe" watcher.py
if errorlevel 1 goto failed
goto end
:failed
echo Could not complete the run. Read the message above and README.md.
echo Python 3.10 or later and internet access are required.
:end
pause
endlocal
