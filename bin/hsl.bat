@echo off
set "HERE=%~dp0.."
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
if "%HSL_PYTHON%"=="" set "HSL_PYTHON=python"
"%HSL_PYTHON%" -m hsl.cli %*
