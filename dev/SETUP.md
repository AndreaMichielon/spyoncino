# Code Quality Setup Guide

## Prerequisites

**First, create your environment using the main run scripts:**

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

## What Gets Installed

### Tools
1. **Ruff** - Fast linter + formatter (replaces black, isort, flake8)
2. **mypy** - Type checker (catches type errors)
3. **pytest** - Test runner (works with your existing tests)
4. **bandit** - Security scanner
5. **pre-commit** - Runs checks automatically on commit

### Files Added/Modified
```
Root directory:
├── pyproject.toml              ← Tool configurations (REQUIRED)
├── .pre-commit-config.yaml     ← Pre-commit hooks (REQUIRED)
├── .gitignore                  ← Updated (REQUIRED)
├── setup_dev.bat/sh            ← Convenience scripts
└── Makefile                    ← Command shortcuts

Updated:
├── .github/workflows/tests.yml ← Now runs quality checks
└── .vscode/settings.json       ← VS Code integration
```

---

## Daily Usage

### Automatic (Recommended)
Just commit normally - hooks run automatically:
```bash
git add .
git commit -m "feat: your message"
```

Pre-commit hooks automatically:
- ✅ Format your code
- ✅ Fix linting issues
- ✅ Check types
- ✅ Scan for security issues

If checks fail, most issues are auto-fixed. Just stage and commit again.

### Manual Commands
```bash
# Fix and format code
ruff check --fix .
ruff format .

# Run tests
pytest
pytest --cov=spyoncino  # with coverage

# Type check
mypy src/

# Or use Make shortcuts
make fix       # Fix + format
make test      # Run tests
make check     # Run all checks
```

---

## VS Code Integration (Optional)

Install these extensions:
1. **Ruff** (charliermarsh.ruff)
2. **Python** (ms-python.python)

Settings are already in `.vscode/settings.json` - code will auto-format on save!

---

## Troubleshooting

**Pre-commit hooks fail on first run**
- Normal! First run downloads hooks
- Most issues are auto-fixed automatically

**"Command not found" errors**
```bash
pre-commit clean
pre-commit install
```

**Skip hooks in emergency**
```bash
git commit --no-verify  # Use sparingly!
```

**Update tools**
```bash
pre-commit autoupdate
pip install -e ".[dev]" --upgrade
```

---

## Configuration

All tools configured in `pyproject.toml`:
- **[tool.ruff]** - Linter/formatter (line length: 100)
- **[tool.mypy]** - Type checker
- **[tool.pytest.ini_options]** - Test settings
- **[tool.bandit]** - Security settings

Pre-commit hooks: `.pre-commit-config.yaml` (must stay in root)

---

## Next: Daily Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for:
- Command reference
- Git workflow
- Code style guidelines
- More troubleshooting
