# Tests

This directory contains tests for the Spyoncino project.

## Structure

- `unit/` - Unit tests for individual modules
- `integration/` - Integration tests for module interactions
- `fixtures/` - Test fixtures and mock data

## Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=spyoncino --cov-report=html

# Run specific test file
pytest tests/unit/test_capture.py
```

## Writing Tests

Please follow these guidelines when writing tests:
- Use descriptive test names
- Include docstrings explaining what is being tested
- Mock external dependencies (camera, network, etc.)
- Keep tests isolated and independent

