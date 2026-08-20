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
rem Redirects to watchdog-stderr.log, NOT supervisor.log: the supervisor opens its own
rem log for append, and on Windows a shell redirect holds an exclusive lock on the same
rem path, so sharing one file makes every pass die with PermissionError.
rem
rem To disable: delete this file.
set "QUANTBOT_CONFIG=E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\config\strategy-v1-2.yaml"
cd /d "E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25"
if not exist "E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\logs" mkdir "E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\logs"
:loop
"E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\.venv\Scripts\python.exe" "E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\scripts\supervisor.py" --watch >> "E:\Documents\GitHub\project_elrond\.claude\worktrees\quantbot-trading-system-2bbc25\logs\watchdog-stderr.log" 2>&1
timeout /t 60 /nobreak > nul
goto loop
