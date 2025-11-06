# Development Reference

Quick reference for daily development tasks.

## Common Commands

### Format & Lint
```bash
ruff check --fix .    # Auto-fix linting issues
ruff format .         # Format code
# Or together:
ruff check --fix . && ruff format .

# Using Make:
make fix
```

### Testing
```bash
pytest                              # Run all tests
pytest --cov=spyoncino             # With coverage
pytest --cov=spyoncino --cov-report=html  # HTML coverage report
pytest tests/unit/test_capture.py  # Specific file
pytest -k "test_camera"            # Match pattern
pytest -v                          # Verbose

# Using Make:
make test
make test-cov
```

### Type Checking
```bash
mypy src/                   # Check all source
mypy src/spyoncino/run.py  # Specific file

# Using Make:
make type-check
```

### Security Scan
```bash
bandit -r src/
bandit -c pyproject.toml -r src/

# Using Make:
make security
```

### Run All Checks
```bash
# Using Make:
make check    # Runs linting, type checking, security

# Or manually:
ruff check .
mypy src/
bandit -r src/
pytest
```

### Pre-commit
```bash
pre-commit run                # On staged files
pre-commit run --all-files    # On all files
pre-commit autoupdate         # Update hook versions
```

### Cleanup
```bash
# Using Make:
make clean

# Or manually (Windows):
rmdir /s /q .pytest_cache .ruff_cache .mypy_cache htmlcov

# Or manually (Linux/Mac):
rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov __pycache__
```

---

## Git Workflow

### Normal Commit
```bash
git add .
git commit -m "feat: add new feature"
# Hooks run automatically
```

### If Hooks Fail
1. Review error messages
2. Most issues are auto-fixed
3. Stage fixes: `git add .`
4. Commit again

### Commit Message Format (Recommended)
```
<type>: <description>

Types: feat, fix, docs, style, refactor, test, chore
```

Examples:
```
feat: add motion detection sensitivity setting
fix: resolve camera connection timeout
docs: update installation instructions
refactor: simplify alert notification logic
test: add tests for security module
```

---

## Code Style Guidelines

### General Rules
- **Line length:** 100 characters (configured in Ruff)
- **Imports:** Organized automatically by Ruff
- **Strings:** Double quotes (configured in Ruff)
- **Type hints:** Use them where helpful
- **Docstrings:** For public functions and classes

### Example
```python
"""Module docstring describing the module."""

from typing import Optional
import logging

logger = logging.getLogger(__name__)


class SecurityCamera:
    """
    Manages a security camera with motion detection.
    
    Args:
        camera_id: Unique identifier for the camera
        rtsp_url: RTSP stream URL
        enabled: Whether monitoring is enabled
    """
    
    def __init__(self, camera_id: str, rtsp_url: str, enabled: bool = True) -> None:
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.enabled = enabled
    
    def start_monitoring(self, interval: int = 30) -> None:
        """
        Start monitoring the camera feed.
        
        Args:
            interval: Check interval in seconds
            
        Raises:
            RuntimeError: If camera cannot be accessed
        """
        if not self.enabled:
            logger.warning(f"Camera {self.camera_id} is disabled")
            return
        
        logger.info(f"Starting monitoring for {self.camera_id}")
        # Implementation...
```

---

## Configuration

### Tool Settings (pyproject.toml)

**Ruff:**
```toml
[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "W", "F", "I", "N", "UP", "B", "C4", "SIM", "PL", "RUF"]
ignore = ["E501", "PLR0913", "PLR2004"]
```

**mypy:**
```toml
[tool.mypy]
python_version = "3.12"
ignore_missing_imports = true
check_untyped_defs = true
```

**pytest:**
```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
addopts = ["--cov=spyoncino"]
```

---

## IDE Setup

### VS Code
Extensions (recommended):
- Ruff (charliermarsh.ruff)
- Python (ms-python.python)
- Mypy Type Checker (ms-python.mypy-type-checker)

Settings already configured in `.vscode/settings.json`:
- Auto-format on save
- Organize imports on save
- Real-time linting

### PyCharm
1. Settings → Tools → External Tools
2. Add Ruff and mypy as external tools
3. Enable "Reformat code" in commit dialog

---

## Troubleshooting

### Ruff Issues

**Line too long:**
```python
# Use line continuation
result = some_function(
    arg1, arg2, arg3,
    arg4, arg5
)

# Or disable for specific line
long_line = "..."  # noqa: E501
```

**Import errors:** Usually auto-fixed by `ruff check --fix`

### mypy Issues

**Missing imports:**
```python
# Add at top of file
# type: ignore

# Or specific line
import some_lib  # type: ignore
```

**Configure in pyproject.toml:**
```toml
[[tool.mypy.overrides]]
module = "problematic_module.*"
ignore_missing_imports = true
```

### pytest Issues

**Tests not found:**
- Check `pythonpath` in pyproject.toml
- Ensure test files start with `test_`

**Import errors in tests:**
- Make sure you have `__init__.py` in test directories
- Check that `src/` is in Python path

### Pre-commit Issues

**Hooks too slow:** First run is slow (downloads), subsequent runs are fast

**Command not found:**
```bash
pre-commit clean
pre-commit install
```

**Want to update:** `pre-commit autoupdate`

---

## Make Commands (Quick Reference)

```bash
make help              # Show all commands
make dev-install       # Setup dev environment
make fix               # Fix and format code
make test              # Run tests
make test-cov          # Tests with HTML coverage
make lint              # Check code (no fixes)
make type-check        # Run mypy
make security          # Security scan
make check             # All checks
make clean             # Remove cache files
make pre-commit        # Run pre-commit on all files
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

1. **Commit often** - Hooks are fast
2. **Let tools auto-fix** - Don't fight them
3. **Use type hints** - Helps catch bugs early
4. **Write tests** - Aim for >80% coverage
5. **Read error messages** - They're usually clear
6. **VS Code users** - Install Ruff extension for real-time feedback
