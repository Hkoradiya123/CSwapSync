@echo off
REM Windows launcher: scripts\cswapsync.cmd <command>
setlocal
set "CSWAPSYNC_HOME=%~dp0.."
"%~dp0..\.venv\Scripts\python.exe" "%~dp0..\src\cli.py" %*
exit /b %ERRORLEVEL%
