# Development Files

This folder contains development documentation.

## Files

### [SETUP.md](SETUP.md)
**Read once** - Initial setup guide
- Prerequisites (`scripts/run.bat` / `scripts/run.sh`)
- Installing dev tools
- What gets installed
- Basic troubleshooting

### [DEVELOPMENT.md](DEVELOPMENT.md)  
**Daily reference** - Keep this handy
- Common commands
- Git workflow
- Code style guidelines
- Configuration details

## Quick Start

```bash
# 1. Create environment (if not done): either
scripts\run.bat      # Windows → spyoncino_env/
./scripts/run.sh     # Linux/Mac → .venv/
# or: uv venv && uv sync --all-extras  → .venv/

# 2. Setup dev tools (works with .venv or spyoncino_env)
dev\setup_dev.bat    # Windows
./dev/setup_dev.sh   # Linux/Mac
```

Done! Now commits run quality checks automatically.
