"""Shared test fixtures."""

import json
from pathlib import Path

import pytest

from custom_components.ynab.const import API_BASE, PLAN_SEGMENT

FIXTURES = Path(__file__).parent / "fixtures"
PLAN = "p"
BASE = f"{API_BASE}/{PLAN_SEGMENT}/{PLAN}"


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


def _register(mock, fixture, *, accounts=None, categories=None, txs=None, month=None,
              k=(1001, 1002, 1003)):
    """Register all four endpoints (any query string matches)."""
    mock.clear_requests()
    a = accounts or fixture("accounts")
    c = categories or fixture("categories")
    t = txs or fixture("transactions")
    m = month or fixture("month")
    a["data"]["server_knowledge"], c["data"]["server_knowledge"], t["data"]["server_knowledge"] = k
    mock.get(f"{BASE}/accounts", json=a)
    mock.get(f"{BASE}/categories", json=c)
    mock.get(f"{BASE}/transactions", json=t)
    mock.get(f"{BASE}/months/current", json=m)
