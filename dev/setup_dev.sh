#!/bin/bash
# Development Environment Setup Script for Linux/Mac
# This script installs all development dependencies and sets up pre-commit hooks

set -e  # Exit on error

echo "========================================"
echo "Spyoncino Development Setup"
echo "========================================"
echo

# Check if virtual environment exists
if [ -d "../spyoncino_env" ]; then
    echo "Virtual environment found."
else
    echo "ERROR: Virtual environment not found!"
    echo "Please create it first using run.sh or manually."
    exit 1
fi

echo "Activating virtual environment..."
source ../spyoncino_env/bin/activate

echo
echo "Installing development dependencies..."
cd ..
uv pip install -e ".[dev]"
cd dev

echo
echo "Setting up pre-commit hooks..."
pre-commit install
pre-commit install --hook-type commit-msg

echo
echo "Running pre-commit on all files (first run may take a while)..."
pre-commit run --all-files || true  # Don't fail on first run

echo
echo "========================================"
echo "Setup Complete!"
echo "========================================"
echo
echo "Your development environment is ready."
echo
echo "Next steps:"
echo "  1. Code as usual"
echo "  2. Commit changes (hooks will run automatically)"
echo "  3. Run tests with: pytest"
echo "  4. Check code with: ruff check ."
echo
echo "See SETUP.md for setup details or DEVELOPMENT.md for daily commands."
echo
