"""Tests for the YNAB HTTP client."""

import logging
from datetime import date

import aiohttp
import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.ynab.client import (
    YnabAuthError,
    YnabClient,
    YnabError,
    YnabNotFoundError,
    YnabRateLimitError,
)
from custom_components.ynab.const import API_BASE, PLAN_SEGMENT

TOKEN = "tok"
PLAN = "p"
BASE = f"{API_BASE}/{PLAN_SEGMENT}/{PLAN}"


@pytest.fixture
async def client(hass, aioclient_mock):
    return YnabClient(async_get_clientsession(hass), TOKEN)


async def test_sends_bearer_token(client, aioclient_mock, fixture):
    aioclient_mock.get(f"{API_BASE}/user", json=fixture("user"))
    user = await client.get_user()
    assert user["id"]
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer tok"


async def test_delta_passes_last_knowledge(client, aioclient_mock, fixture):
    aioclient_mock.get(f"{BASE}/transactions?last_knowledge_of_server=42", json=fixture("transactions"),
    )
    txs, knowledge = await client.get_transactions(PLAN, since=42, since_date=None)
    assert isinstance(txs, list) and len(txs) == 6
    assert knowledge == 1003


async def test_transactions_since_date(client, aioclient_mock, fixture):
    aioclient_mock.get(f"{BASE}/transactions?since_date=2026-10-01", json=fixture("transactions")
    )
    txs, _ = await client.get_transactions(
        PLAN, since=None, since_date=date(2026, 10, 1)
    )
    assert txs


async def test_list_plans_accounts_categories_month(client, aioclient_mock, fixture):
    aioclient_mock.get(f"{API_BASE}/{PLAN_SEGMENT}", json=fixture("plans"))
    aioclient_mock.get(f"{BASE}/accounts", json=fixture("accounts"))
    aioclient_mock.get(f"{BASE}/categories", json=fixture("categories"))
    aioclient_mock.get(f"{BASE}/months/current", json=fixture("month"))
    plans = await client.list_plans()
    assert plans[0]["currency_format"]["iso_code"] == "USD"
    accounts, ak = await client.get_accounts(PLAN, since=None)
    assert any(a["name"] == "Apple Savings" for a in accounts) and ak == 1001
    groups, ck = await client.get_categories(PLAN, since=None)
    assert any(g["name"] == "Bills" for g in groups) and ck == 1002
    month = await client.get_month(PLAN, "current")
    assert month["to_be_budgeted"] == 1250000


async def test_401_raises_auth_error_without_token(client, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/user", status=401, json={
            "error": {"id": "401", "name": "unauthorized", "detail": "Unauthorized"}
        },
    )
    with pytest.raises(YnabAuthError) as err:
        await client.get_user()
    assert "tok" not in str(err.value)
    assert "Unauthorized" in str(err.value)


async def test_404_raises_not_found(client, aioclient_mock):
    aioclient_mock.get(f"{BASE}/months/2020-01-01", status=404, json={"error": {"id": "404.2", "name": "resource_not_found", "detail": "Resource not found"}},
    )
    with pytest.raises(YnabNotFoundError):
        await client.get_month(PLAN, "2020-01-01")


async def test_429_raises_rate_limit_error(client, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/user", status=429, json={"error": {"id": "429", "name": "too_many_requests", "detail": "Too many requests"}},
    )
    with pytest.raises(YnabRateLimitError):
        await client.get_user()
    # No retry inside the client.
    assert aioclient_mock.call_count == 1


async def test_network_error_wrapped(client, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/user", exc=aiohttp.ClientConnectionError("boom"))
    with pytest.raises(YnabError) as err:
        await client.get_user()
    assert "tok" not in str(err.value)


async def test_failed_request_logs_no_token(client, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG)
    aioclient_mock.get(f"{API_BASE}/user", status=500, text="oops")
    with pytest.raises(YnabError) as err:
        await client.get_user()
    assert "tok" not in str(err.value)
    assert caplog.records
    assert all(TOKEN not in r.getMessage() for r in caplog.records)
    assert all(TOKEN not in str(r.__dict__) for r in caplog.records)


async def test_requests_last_hour_counts_and_expires(client, aioclient_mock, fixture, freezer):
    for _ in range(3):
        aioclient_mock.get(f"{API_BASE}/user", json=fixture("user"))
    for _ in range(3):
        await client.get_user()
    assert client.requests_last_hour == 3
    freezer.tick(3601)
    assert client.requests_last_hour == 0


async def test_update_transactions_sends_bulk_patch(client, aioclient_mock, fixture):
    aioclient_mock.patch(f"{BASE}/transactions", json={"data": {"transactions": [{"id": "t1", "category_id": "c1"}]}},
    )
    updates = [{"id": "t1", "category_id": "c1", "approved": True}]
    result = await client.update_transactions(PLAN, updates)
    assert result == [{"id": "t1", "category_id": "c1"}]
    method, _, body, _ = aioclient_mock.mock_calls[0]
    assert method == "PATCH"
    assert body == {"transactions": updates}


async def test_set_category_budgeted(client, aioclient_mock):
    aioclient_mock.patch(f"{BASE}/months/2026-11-01/categories/c1", json={"data": {"category": {"id": "c1", "budgeted": 5000}}},
    )
    cat = await client.set_category_budgeted(PLAN, "2026-11-01", "c1", 5000)
    assert cat["budgeted"] == 5000
    assert aioclient_mock.mock_calls[0][2] == {"category": {"budgeted": 5000}}


def _fill(client, n):
    import time

    client._request_times.extend([time.time()] * n)


async def test_budget_refuses_writes_and_reads_but_not_polls(client, aioclient_mock, fixture):
    aioclient_mock.patch(f"{BASE}/transactions", json={"data": {"transactions": []}})
    aioclient_mock.get(f"{BASE}/months/current", json=fixture("month"))
    aioclient_mock.get(f"{BASE}/months/2026-11-01", json=fixture("month"))
    _fill(client, 180)
    with pytest.raises(YnabRateLimitError, match="Local request budget"):
        await client.update_transactions(PLAN, [{"id": "x", "memo": "m"}])
    with pytest.raises(YnabRateLimitError, match="Local request budget"):
        await client.get_month(PLAN, "2026-11-01")
    assert aioclient_mock.call_count == 0
    await client.get_month(PLAN, "current", poll=True)
    assert aioclient_mock.call_count == 1


async def test_budget_refuses_everything_at_200(client, aioclient_mock, fixture):
    aioclient_mock.get(f"{BASE}/months/current", json=fixture("month"))
    _fill(client, 200)
    with pytest.raises(YnabRateLimitError, match="Local request budget"):
        await client.get_month(PLAN, "current", poll=True)
    assert aioclient_mock.call_count == 0


async def test_below_budget_requests_go_out(client, aioclient_mock, fixture):
    aioclient_mock.get(f"{BASE}/months/2026-11-01", json=fixture("month"))
    _fill(client, 179)
    await client.get_month(PLAN, "2026-11-01")
    assert aioclient_mock.call_count == 1
