@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Switch the console to UTF-8. The Python side reconfigures its own stdout/stderr to
rem UTF-8, and the console must agree on the codepage or Chinese text comes out as
rem mojibake. Safe here because this file is ASCII-only (see note below).
chcp 65001 >nul 2>nul

title SmartCrawler Launcher

rem ===========================================================================
rem  SmartCrawler Launcher  (double-click to start the web console)
rem ---------------------------------------------------------------------------
rem  This file is intentionally ASCII-only. cmd.exe reads .bat files byte by
rem  byte using the console's OEM codepage, so multi-byte (e.g. Chinese) text
rem  inside a .bat corrupts the parser. All localized text therefore lives in
rem  scripts\launcher.py, which is read as UTF-8 by Python.
rem
rem  Options (all optional -- double-clicking works with no arguments):
rem    start.bat --port 9000      use another port
rem    start.bat --no-browser     do not open a browser
rem    start.bat --check          environment check only, do not start
rem    start.bat --reload         auto-reload on code change (development)
rem ===========================================================================

rem Always run from the script's own directory (double-click sets system32).
cd /d "%~dp0"

set "VENV_PY=%CD%\.venv\Scripts\python.exe"
set "ARGS="

rem ---------------------------------------------------------------------------
rem Collect arguments to forward to the Python launcher.
rem ---------------------------------------------------------------------------
:parse_args
if "%~1"=="" goto args_done
if /i "%~1"=="-h"     goto usage
if /i "%~1"=="--help" goto usage
set "ARGS=%ARGS% %~1"
if /i "%~1"=="--port" (
    set "ARGS=%ARGS% %~2"
    shift
)
shift
goto parse_args

:usage
echo Usage: start.bat [--port 8322] [--no-browser] [--check] [--reload]
echo.
echo   --port N        listen port (default: SC_API__PORT from .env, else 8322)
echo   --no-browser    do not open the browser automatically
echo   --check         only verify the environment, do not start the service
echo   --reload        auto-reload on code change (development)
echo.
pause
exit /b 0

:args_done

rem ---------------------------------------------------------------------------
rem Locate a Python interpreter (prefer the project venv).
rem ---------------------------------------------------------------------------
set "PY="
if exist "%VENV_PY%" (
    set "PY=%VENV_PY%"
    goto run
)

rem py launcher is the most reliable: it lists installed versions regardless of PATH.
where py >nul 2>nul
if not errorlevel 1 (
    py -3 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=py -3"
        goto run
    )
)
where python >nul 2>nul
if not errorlevel 1 (
    python -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
        goto run
    )
)

echo.
echo  [ERROR] No usable Python found.
echo.
echo  Please install Python 3.10+ (3.12 recommended):
echo      https://www.python.org/downloads/
echo  Make sure to tick "Add python.exe to PATH" during installation.
echo.
pause
exit /b 1

rem ---------------------------------------------------------------------------
rem Hand over to the Python launcher, which performs all checks and starts
rem the service. It is UTF-8, so it can print localized messages safely.
rem ---------------------------------------------------------------------------
:run
%PY% "scripts\launcher.py" %ARGS%
set "EXITCODE=%ERRORLEVEL%"

rem ---------------------------------------------------------------------------
rem Final safety net: sweep ORPHANED browser processes this framework left behind.
rem
rem scripts\launcher.py already does this from a watchdog thread, but if the
rem console is closed hard (window X, taskkill, IDE stop) the launcher itself
rem may never get to run. This line is cheap and only ever touches:
rem   (a) processes whose command line points at THIS project's .browsers dir, and
rem   (b) processes whose parent chain has no live owner left (true orphans).
rem "b" matters: without it, closing one instance would kill the browsers of
rem another instance still running from the same project.
rem ---------------------------------------------------------------------------
%PY% -c "import sys; sys.path.insert(0, r'%CD%'); from smartcrawler.runtime import kill_orphan_processes; n = kill_orphan_processes(reason='start.bat exit'); print('  [cleanup] killed %d orphan browser process(es)' % n) if n else None" 2>nul

if not "%EXITCODE%"=="0" (
    echo.
    echo  [ERROR] Launcher exited with code %EXITCODE%.
    echo.
    pause
    exit /b %EXITCODE%
)

exit /b 0
