@echo off
REM Development Environment Setup Script for Windows
REM This script installs all development dependencies and sets up pre-commit hooks

echo ========================================
echo Spyoncino Development Setup
echo ========================================
echo.

REM Prefer .venv (e.g. uv), else spyoncino_env (scripts\run.bat / scripts\run.sh)
if exist "..\.venv\Scripts\activate.bat" (
    echo Virtual environment found: .venv
    call ..\.venv\Scripts\activate.bat
) else if exist "..\spyoncino_env\Scripts\activate.bat" (
    echo Virtual environment found: spyoncino_env
    call ..\spyoncino_env\Scripts\activate.bat
) else (
    echo ERROR: No virtual environment found!
    echo Create one with: scripts\run.bat  OR  uv venv ^&^& uv sync --all-extras
    pause
    exit /b 1
)

echo.
echo Installing development dependencies...
cd ..
uv pip install -e ".[dev]"
cd dev

if errorlevel 1 (
    echo ERROR: Failed to install dependencies
    pause
    exit /b 1
)

echo.
echo Setting up pre-commit hooks...
pre-commit install
pre-commit install --hook-type commit-msg

if errorlevel 1 (
    echo ERROR: Failed to install pre-commit hooks
    pause
    exit /b 1
)

echo.
echo Running pre-commit on all files (first run may take a while)...
pre-commit run --all-files

echo.
echo ========================================
echo Setup Complete!
echo ========================================
echo.
echo Your development environment is ready.
echo.
echo Next steps:
echo   1. Code as usual
echo   2. Commit changes (hooks will run automatically)
echo   3. Run tests with: pytest
echo   4. Check code with: ruff check .
echo.
echo See SETUP.md for setup details or DEVELOPMENT.md for daily commands.
echo.
pause
