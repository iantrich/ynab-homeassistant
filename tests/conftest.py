"""Shared test fixtures."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load tests/fixtures/<name>.json."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to load custom_components in every test."""
    yield


@pytest.fixture
def fixture():
    """Return the fixture loader."""
    return load_fixture
