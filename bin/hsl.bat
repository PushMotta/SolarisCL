@echo off
set "HERE=%~dp0.."
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
rem The standalone zip ships its own runtime in python\ -- it wins, so the
rem tool runs identically on machines with no Python at all. Delete that
rem folder (or use the slim zip) to run your own interpreter instead.
if exist "%HERE%\python\python.exe" set "HSL_PYTHON=%HERE%\python\python.exe"
if "%HSL_PYTHON%"=="" set "HSL_PYTHON=python"
"%HSL_PYTHON%" -m hsl.cli %*
