"""Basic tests for configuration loading."""

import json
import os
import tempfile

import pytest


def test_load_valid_json():
    """Test that valid JSON can be loaded."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        test_config = {"test_key": "test_value"}
        json.dump(test_config, f)
        temp_path = f.name

    try:
        with open(temp_path) as f:
            loaded = json.load(f)
        assert loaded["test_key"] == "test_value"
    finally:
        os.unlink(temp_path)


def test_invalid_json_raises_error():
    """Test that invalid JSON raises an error."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{ invalid json }")
        temp_path = f.name

    try:
        with pytest.raises(json.JSONDecodeError), open(temp_path) as f:
            json.load(f)
    finally:
        os.unlink(temp_path)


# TODO: Add actual config validation tests
# - Test setting.json schema validation
# - Test secrets.json validation
# - Test default values
