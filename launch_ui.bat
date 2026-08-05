@echo off
cd /d "%~dp0"
rem The standalone zip ships its own runtime in python\ -- it wins, so the
rem window opens on machines with no Python at all. Delete that folder (or
rem use the slim zip) to run your own interpreter instead.
if exist "%~dp0python\python.exe" set "HSL_PYTHON=%~dp0python\python.exe"
if "%HSL_PYTHON%"=="" set "HSL_PYTHON=python"
"%HSL_PYTHON%" -m hsl.ui
pause
