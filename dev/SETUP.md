# Code Quality Setup Guide

## Prerequisites

**First, create your environment using the main run scripts (see the full quick-start flow in the [README](../README.md#quick-start)):**

### Windows
```bash
run.bat
```

### Linux/Mac
```bash
./run.sh
```

This creates the `spyoncino_env/` virtual environment with all dependencies.

---

## Quick Dev Setup (After Environment is Created)

### Windows
```bash
dev\setup_dev.bat
```

### Linux/Mac
```bash
./dev/setup_dev.sh
```

### Or Manual
```bash
# 1. Activate your environment
spyoncino_env\Scripts\activate  # Windows
source spyoncino_env/bin/activate  # Linux/Mac

# 2. Install dev dependencies
pip install -e ".[dev]"

# 3. Setup pre-commit hooks
pre-commit install
```

**That's it!** Now when you commit, code quality checks run automatically.

---

## What's Included

- **Tooling**: Ruff (lint/format), mypy, pytest, bandit, pre-commit
- **Configs & scripts**:
  - `pyproject.toml` – shared settings
  - `.pre-commit-config.yaml` – hook definitions
  - `.gitignore`, `setup_dev.*`, `Makefile`
  - `.github/workflows/tests.yml`, `.vscode/settings.json`

---

## Usage

**Commit flow**
```bash
git add .
git commit -m "feat: your message"
```
Hooks format, lint, type-check, and run security scans automatically. Fixes are usually applied for you—just restage if needed.

**Manual checks**
```bash
ruff check --fix .
ruff format .
pytest            # add --cov=spyoncino for coverage
mypy src/
make check        # runs the full suite
```

---

## VS Code Integration (Optional)

Install these extensions:
1. **Ruff** (charliermarsh.ruff)
2. **Python** (ms-python.python)

Settings are already in `.vscode/settings.json` - code will auto-format on save!

---

## Troubleshooting

| Issue | Quick fix |
|-------|-----------|
| Hooks fail on first run | Retry the commit; dependencies finish downloading. |
| `command not found` | `pre-commit clean && pre-commit install` |
| Need to bypass hooks | `git commit --no-verify` *(avoid unless blocked)* |
| Update tooling | `pre-commit autoupdate && pip install -e ".[dev]" --upgrade` |

---

## Configuration

All tools configured in `pyproject.toml`:
- **[tool.ruff]** - Linter/formatter (line length: 100)
- **[tool.mypy]** - Type checker
- **[tool.pytest.ini_options]** - Test settings
- **[tool.bandit]** - Security settings

Pre-commit hooks: `.pre-commit-config.yaml` (must stay in root)

---

## Next: Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for:
- Command reference
- Git workflow
- Code style guidelines
- More troubleshooting
