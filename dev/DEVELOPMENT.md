# Development Reference

Cheat sheet for day-to-day development.

## Daily Loop
1. Activate your environment (`spyoncino_env` / `.venv`).
2. Keep code tidy: `make fix` *(or `ruff check --fix . && ruff format .`)*.
3. Run tests: `make test` *(add `--cov=spyoncino` when needed)*.
4. Optional deep checks: `make type-check`, `make security`, `make check`.
5. Commit with a conventional message (`feat: ...`, `fix: ...`, etc.).

## Command Reference
| Task | Direct command | Make target | Notes |
|------|----------------|-------------|-------|
| Format & lint | `ruff check --fix .` / `ruff format .` | `make fix` | Auto-fixes most issues |
| Run tests | `pytest` | `make test` | Use `--cov=spyoncino` for coverage, `-k "pattern"` to filter |
| Type check | `mypy src/` | `make type-check` | Run on a module: `mypy path/to/file.py` |
| Security scan | `bandit -c pyproject.toml -r src/` | `make security` | Needs `bandit` on PATH (e.g. `uv tool install bandit[toml]`); pre-commit also runs Bandit |
| Lint + types + security | `ruff check . && mypy src/ && bandit -c pyproject.toml -r src/` | `make check` | No pytest (use `make test`) |
| Tests + checks | — | `make check && make test` | Reasonable pre-PR pass |
| Pre-commit hooks | `pre-commit run --all-files` | `make pre-commit` | Hooks also run automatically on commit |
| Clean caches | See below | `make clean` | Removes `.pytest_cache`, `.ruff_cache`, `.mypy_cache`, `htmlcov` |

### Targeted Commands
```bash
# Coverage report
pytest --cov=spyoncino --cov-report=html

# Specific test file or keyword
pytest tests/unit/test_capture.py
pytest -k "test_camera"

# Manual cache cleanup
rmdir /s /q .pytest_cache .ruff_cache .mypy_cache htmlcov           # Windows
rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov __pycache__    # Linux/Mac
```

---

## Git Workflow
- `git add .` → `git commit -m "feat: add motion detection toggle"` → hooks run automatically.
- If hooks fail: read the output, most fixes are staged automatically; run `git add .` and commit again.
- Message format: `<type>: <summary>` where type ∈ {feat, fix, docs, style, refactor, test, chore}.

---

## Code Style Guidelines
- Line length 100 (enforced by Ruff)
- Imports auto-organized by Ruff
- Prefer double quotes, type hints, and docstrings for public APIs
- Let Ruff auto-fix style issues whenever possible

---

## Configuration

### Tool settings

`pyproject.toml` currently lists **optional dev dependencies** (`[project.optional-dependencies.dev]`). There are no `[tool.ruff]` / `[tool.mypy]` / `[tool.pytest.ini_options]` blocks yet, so Ruff and mypy use their defaults when you run the Makefile or CLI.

You can add those sections when you want pinned rules (for example `testpaths`, `pythonpath`, coverage defaults). **Bandit** is invoked with `bandit -c pyproject.toml`; add `[tool.bandit]` when you want custom skips or severity.

---

## IDE Setup
- **VS Code**: install Ruff + Python (and optionally mypy) extensions. Workspace settings already enable format-on-save and organized imports.
- **PyCharm**: add Ruff/mypy as external tools and enable “Reformat code” in the commit dialog.

---

## Troubleshooting
| Issue | Quick fix |
|-------|-----------|
| Ruff import or style errors | `ruff check --fix .`; rerun to auto-apply fixes |
| Intentional long line | Append `# noqa: E501` (sparingly) |
| mypy missing imports | Add `# type: ignore` to the import or configure `[[tool.mypy.overrides]]` |
| Tests not discovered | Ensure files start with `test_` and `tests/` has `__init__.py` |
| Import errors in tests | Install editable: `uv pip install -e .` or `uv sync --all-extras` so `spyoncino` resolves |
| Hooks slow or missing | First run downloads deps; otherwise `pre-commit clean && pre-commit install` |
| Update tooling | `pre-commit autoupdate && uv sync --all-extras` |

---

## Make Commands (Quick Reference)
```bash
make help          # Show all commands
make dev-install   # Setup dev environment
make fix           # Fix and format code
make test          # Run tests
make test-cov      # Tests with HTML coverage
make lint          # Check code (no fixes)
make type-check    # Run mypy
make security      # Security scan
make check         # All checks
make clean         # Remove cache files
make pre-commit    # Run pre-commit on all files
```

---

## Resources
- [Ruff Documentation](https://docs.astral.sh/ruff/)
- [mypy Documentation](https://mypy.readthedocs.io/)
- [pytest Documentation](https://docs.pytest.org/)
- [pre-commit Documentation](https://pre-commit.com/)
- [PEP 8 Style Guide](https://peps.python.org/pep-0008/)

---

## Quick Tips
- Commit often—hooks are fast
- Let tools auto-fix before editing manually
- Type hints catch bugs early
- Aim for >80 % coverage on critical code
- Read error messages—they’re usually actionable
- VS Code users: Ruff extension gives instant feedback
