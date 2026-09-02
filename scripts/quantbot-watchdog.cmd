@echo off
rem QuantBot watchdog. Runs the supervisor, which starts the daemon if it is absent.
rem
rem This used to loop the daemon directly. That spins: if a daemon is already holding
rem the lock the relaunch exits at once and the loop retries every 60 seconds forever.
rem The supervisor checks liveness by the lock before restarting anything, so it is
rem quiet when healthy. It also refuses to clear the kill switch or resolve a
rem reconciliation failure - it restarts a dead process, it never overrules a
rem deliberate stop.
rem
rem Every path is derived from this file's own location (#190). It used to name an
rem absolute checkout on a drive that no longer exists on the coordinator: a copy of
rem this file left in a Startup folder on an older host would have resurrected a
rem coordinator from a stale checkout against the same paper account, which is the
rem second-writer hazard #190 exists to close. A self-relative script can only ever
rem start the checkout it was copied from.
rem
rem Redirects to watchdog-stderr.log, NOT supervisor.log: the supervisor opens its own
rem log for append, and on Windows a shell redirect holds an exclusive lock on the same
rem path, so sharing one file makes every pass die with PermissionError.
rem
rem To disable: delete this file.
setlocal
pushd "%~dp0.." || exit /b 1
set "REPO=%CD%"

rem Fail loudly rather than looping in silence. A copy of this file that has been moved
rem away from its checkout has no supervisor to run, and a watchdog that logs nothing is
rem indistinguishable from a healthy one.
if not exist "%REPO%\scripts\supervisor.py" (
    echo Not a QuantBot checkout: "%REPO%" has no scripts\supervisor.py. 1>&2
    popd
    exit /b 1
)
if not exist "%REPO%\.venv\Scripts\python.exe" (
    echo No environment at "%REPO%\.venv". Run: uv sync --frozen 1>&2
    popd
    exit /b 1
)

set "QUANTBOT_CONFIG=%REPO%\config\strategy-v1-2.yaml"
if not exist "%REPO%\logs" mkdir "%REPO%\logs"
:loop
"%REPO%\.venv\Scripts\python.exe" "%REPO%\scripts\supervisor.py" --watch >> "%REPO%\logs\watchdog-stderr.log" 2>&1
timeout /t 60 /nobreak > nul
goto loop
