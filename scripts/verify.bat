@echo off
rem Execution policy applies only to this invocation of the reviewed local script.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0verify.ps1" %*
exit /b %errorlevel%
