# Code Quality Setup Guide

## Prerequisites

**First, create your environment using the main run scripts (see the full quick-start flow in the [README](../README.md#quick-start)):**

### Windows
```bat
scripts\run.bat
```

### Linux/Mac
```bash
./scripts/run.sh
```

On Windows the launcher uses **`spyoncino_env/`**; on Linux/macOS it uses **`.venv/`**. Alternatively, `uv venv` then `uv sync --all-extras` creates **`.venv/`** (what many contributors use).

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
# 1. Activate your environment (.venv or spyoncino_env)
.\.venv\Scripts\activate             # Windows (.venv / uv)
spyoncino_env\Scripts\activate      # Windows (scripts\run.bat)
source .venv/bin/activate          # Linux/macOS (.venv / ./scripts/run.sh)
source spyoncino_env/bin/activate  # Linux/macOS — only if you created this venv manually

# 2. Install dev dependencies (same as make dev-install)
uv sync --all-extras

# 3. Setup pre-commit hooks
pre-commit install
pre-commit install --hook-type commit-msg
```

**That's it!** Now when you commit, code quality checks run automatically.

---

## What's Included

- **Tooling**: Ruff (lint/format), mypy, pytest, pytest-cov, pre-commit; **Bandit** runs via pre-commit hooks. For `make security` / `make check`, install Bandit on your PATH (e.g. `uv tool install bandit[toml]` or `pip install bandit[toml]`).
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
make check        # ruff + mypy + bandit (no pytest; use make test for tests)
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
| Update tooling | `pre-commit autoupdate && uv sync --all-extras` |

---

## Configuration

- **`pyproject.toml`** – project metadata and `[project.optional-dependencies.dev]` (ruff, mypy, pytest, pytest-cov). Add optional `[tool.ruff]`, `[tool.mypy]`, `[tool.pytest.ini_options]`, `[tool.bandit]` when you want pinned tool settings; until then Ruff/mypy use their defaults. Bandit is not listed under `[dev]` (pre-commit supplies it for commits); install separately if you use `make security`.
- **`.pre-commit-config.yaml`** – hook definitions (must stay in repo root).

`Makefile` targets pass explicit paths (for example `mypy src/`, `bandit -c pyproject.toml -r src/`).

---

## Next: Development

See [DEVELOPMENT.md](DEVELOPMENT.md) for:
- Command reference
- Git workflow
- Code style guidelines
- More troubleshooting
