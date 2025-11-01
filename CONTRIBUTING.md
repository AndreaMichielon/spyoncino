# Contributing to Spyoncino

Thank you for your interest in contributing to Spyoncino! This document provides guidelines and information for contributors.

## Getting Started

### Prerequisites
- Python 3.12+
- Git
- Basic understanding of computer vision and AI concepts

### Development Setup

1. **Fork and clone the repository**
   ```bash
   git clone https://github.com/YOUR_USERNAME/spyoncino.git
   cd spyoncino
   ```

2. **Create a virtual environment**
   ```bash
   uv venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install in development mode**
   ```bash
   uv pip install -e .
   ```

4. **Install development dependencies**
   ```bash
   uv pip install pytest pytest-cov ruff mypy
   ```

## How to Contribute

### Reporting Bugs

If you find a bug, please create an issue with:
- Clear description of the problem
- Steps to reproduce
- Expected vs actual behavior
- Your environment (OS, Python version, camera type)
- Relevant logs or error messages

### Suggesting Features

Feature suggestions are welcome! Please create an issue with:
- Clear description of the feature
- Use case and benefits
- Any implementation ideas (optional)

### Pull Requests

1. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

2. **Make your changes**
   - Write clear, documented code
   - Follow the existing code style
   - Add tests for new functionality
   - Update documentation as needed

3. **Test your changes**
   ```bash
   # Run tests
   pytest
   
   # Check code style
   ruff check .
   
   # Type checking
   mypy src/spyoncino
   ```

4. **Commit your changes**
   ```bash
   git add .
   git commit -m "Add feature: brief description"
   ```
   
   Use clear commit messages that explain what and why, not just how.

5. **Push and create a pull request**
   ```bash
   git push origin feature/your-feature-name
   ```
   
   Then create a pull request on GitHub with:
   - Description of changes
   - Related issue numbers (if any)
   - Screenshots/GIFs for UI changes

## Code Guidelines

### Style
- Follow PEP 8 style guide
- Use type hints for function parameters and returns
- Keep functions focused and small
- Use descriptive variable names

### Documentation
- Add docstrings to all public functions and classes
- Update README.md if user-facing changes
- Comment complex logic

### Testing
- Write tests for new features
- Ensure existing tests pass
- Aim for good test coverage on critical paths

## Project Structure

```
spyoncino/
├── src/spyoncino/      # Main source code
│   ├── run.py          # Entry point
│   ├── security.py     # AI detection logic
│   ├── bot.py          # Telegram bot
│   ├── capture.py      # Camera interface
│   ├── manager.py      # Event management
│   └── analytics.py    # Analytics and stats
├── config/             # Configuration files
├── tests/              # Test suite
├── recordings/         # Runtime data (gitignored)
└── docs/               # Documentation (future)
```

## Development Roadmap

Check [TODO.md](TODO.md) for the current development roadmap and planned features.

## Questions?

Feel free to:
- Open an issue for questions
- Start a discussion in GitHub Discussions
- Contact the maintainer

## Code of Conduct

Be respectful, constructive, and professional. This is a learning and collaborative environment.

---

Thank you for contributing to Spyoncino! 🎉

