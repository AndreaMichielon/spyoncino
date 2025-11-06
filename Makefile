.PHONY: help install dev-install test test-cov lint format type-check security check fix clean pre-commit

help:  ## Show this help message
	@echo "Spyoncino Development Commands"
	@echo "==============================="
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install:  ## Install production dependencies
	uv sync

dev-install:  ## Install development dependencies and setup pre-commit
	uv sync --all-extras
	pre-commit install
	pre-commit install --hook-type commit-msg

test:  ## Run tests
	pytest

test-cov:  ## Run tests with coverage report
	pytest --cov=spyoncino --cov-report=term-missing --cov-report=html

lint:  ## Check code with ruff (no fixes)
	ruff check .

format:  ## Format code with ruff
	ruff format .

type-check:  ## Run type checking with mypy
	mypy src/

security:  ## Run security checks with bandit
	bandit -c pyproject.toml -r src/

check:  ## Run all checks (lint, type, security)
	@echo "Running linter..."
	ruff check .
	@echo "\nRunning type checker..."
	mypy src/
	@echo "\nRunning security checker..."
	bandit -c pyproject.toml -r src/
	@echo "\n✅ All checks passed!"

fix:  ## Auto-fix linting issues and format code
	ruff check --fix .
	ruff format .

clean:  ## Remove cache and build artifacts
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

pre-commit:  ## Run pre-commit on all files
	pre-commit run --all-files

pre-commit-update:  ## Update pre-commit hooks
	pre-commit autoupdate

run:  ## Run the application (Linux/Mac)
	./run.sh

run-win:  ## Run the application (Windows)
	run.bat
