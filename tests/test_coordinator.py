"""Tests for the delta-sync coordinator."""

import copy
from decimal import Decimal

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.ynab.client import YnabClient
from custom_components.ynab.const import (
    API_BASE,
    EVENT_READY_TO_ASSIGN_INCREASED,
    EVENT_TRANSACTION_IMPORTED,
    PLAN_SEGMENT,
)
from custom_components.ynab.coordinator import YnabCoordinator
from custom_components.ynab.store import YnabStore
from tests.conftest import _register

PLAN = "p"
BASE = f"{API_BASE}/{PLAN_SEGMENT}/{PLAN}"


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")


async def _poll(coord):
    """Run one sync and store the result like DataUpdateCoordinator does."""
    coord.data = await coord._async_update_data()
    return coord.data


async def _make(hass, store=None):
    entry = MockConfigEntry(
        domain="ynab", data={"api_token": "tok", "plan_id": PLAN}, options={}
    )
    entry.add_to_hass(hass)
    if store is None:
        store = YnabStore(hass, entry.entry_id)
        await store.async_load()
    client = YnabClient(async_get_clientsession(hass), "tok")
    return YnabCoordinator(hass, entry, client, store, PLAN, "USD"), entry


async def test_first_refresh_fires_no_imported_events(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    events = async_capture_events(hass, EVENT_TRANSACTION_IMPORTED)
    coord, _ = await _make(hass)
    assert not coord.store.initialized
    data = await _poll(coord)
    await hass.async_block_till_done()
    assert events == []
    assert coord.store.initialized is True
    assert "t-withdrawal" in data.transactions
    assert "t-deleted" not in data.transactions
    assert "acc-old" not in data.accounts
    assert data.ready_to_assign == Decimal(1250)
    assert data.currency == "USD"
    assert coord.store.knowledge == {
        "accounts": 1001, "categories": 1002, "transactions": 1003
    }
    # full fetch: no knowledge param, since_date = today - 120d
    tx_call = [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(f"{BASE}/transactions")][0]
    assert tx_call[1].query == {"since_date": "2026-07-08"}


async def test_new_transaction_fires_imported_event_with_payload(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    events = async_capture_events(hass, EVENT_TRANSACTION_IMPORTED)
    coord, entry = await _make(hass)
    await _poll(coord)

    delta = {"data": {"transactions": [
        {**copy.deepcopy(fixture("transactions")["data"]["transactions"][0]),
         "id": "t-new", "memo": "m", "import_payee_name": "WITHDRAWAL X"}
    ]}}
    _register(aioclient_mock, fixture, txs=delta, k=(1101, 1102, 1103))
    await _poll(coord)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data == {
        "entry_id": entry.entry_id,
        "transaction_id": "t-new",
        "account_id": "acc-savings",
        "account_name": "Apple Savings",
        "import_payee_name": "WITHDRAWAL X",
        "payee_name": "Withdrawal",
        "category_id": None,
        "category_name": None,
        "amount": -623.98,
        "direction": "outflow",
        "date": "2026-11-03",
        "day": 3,
        "approved": False,
        "cleared": "uncleared",
        "memo": "m",
    }
    # delta request used stored knowledge
    tx_call = [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(f"{BASE}/transactions")][-1]
    assert tx_call[1].query == {"last_knowledge_of_server": "1003"}
    assert coord.store.knowledge["transactions"] == 1103


async def test_restart_does_not_refire(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    coord, entry = await _make(hass)
    await _poll(coord)
    await coord.store.async_save_now()

    reloaded = YnabStore(hass, entry.entry_id)
    await reloaded.async_load()
    assert reloaded.initialized
    events = async_capture_events(hass, EVENT_TRANSACTION_IMPORTED)
    coord2, _ = await _make(hass, reloaded)
    _register(aioclient_mock, fixture)
    await _poll(coord2)
    await hass.async_block_till_done()
    assert events == []


async def test_deleted_transaction_removed_without_event(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    events = async_capture_events(hass, EVENT_TRANSACTION_IMPORTED)
    coord, _ = await _make(hass)
    await _poll(coord)
    assert "t-mortgage" in coord.data.transactions

    gone = copy.deepcopy(fixture("transactions")["data"]["transactions"][2])
    assert gone["id"] == "t-mortgage"
    gone["deleted"] = True
    _register(aioclient_mock, fixture, txs={"data": {"transactions": [gone]}}, k=(2001, 2002, 2003))
    new = await _poll(coord)
    await hass.async_block_till_done()
    assert "t-mortgage" not in new.transactions
    assert "t-mortgage" not in coord.store.seen_transaction_ids
    assert events == []


async def test_rate_limit_keeps_knowledge(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    coord, _ = await _make(hass)
    await _poll(coord)
    before = dict(coord.store.knowledge)

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/accounts", json=fixture("accounts"))
    aioclient_mock.get(f"{BASE}/categories", status=429, json={"error": {"detail": "slow"}})
    with pytest.raises(UpdateFailed, match="429"):
        await coord._async_update_data()
    assert coord.store.knowledge == before


async def test_auth_error_triggers_reauth(hass, aioclient_mock, fixture):
    aioclient_mock.get(f"{BASE}/accounts", status=401, json={"error": {"detail": "bad"}})
    coord, _ = await _make(hass)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_ready_to_assign_increase_event(hass, aioclient_mock, fixture):
    month = fixture("month")
    month["data"]["month"]["to_be_budgeted"] = 100000
    _register(aioclient_mock, fixture, month=month)
    events = async_capture_events(hass, EVENT_READY_TO_ASSIGN_INCREASED)
    coord, entry = await _make(hass)
    await _poll(coord)
    assert events == []  # not on first refresh

    month = fixture("month")
    month["data"]["month"]["to_be_budgeted"] = 1600000
    _register(aioclient_mock, fixture, month=month)
    await _poll(coord)
    await hass.async_block_till_done()
    assert [e.data for e in events] == [
        {"entry_id": entry.entry_id, "old": 100.0, "new": 1600.0, "delta": 1500.0}
    ]

    month = fixture("month")
    month["data"]["month"]["to_be_budgeted"] = 50000
    _register(aioclient_mock, fixture, month=month)
    await _poll(coord)
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_requests_per_poll_is_four(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    coord, _ = await _make(hass)
    await _poll(coord)
    assert aioclient_mock.call_count == 4
    await _poll(coord)
    assert aioclient_mock.call_count == 8


async def test_low_state_and_eligibility(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    coord, _ = await _make(hass)
    await _poll(coord)
    ids = {c.id for c in coord.eligible_categories()}
    assert "c-oldstuff" not in ids  # hidden
    assert "c-ccpay" not in ids and "c-rta" not in ids  # excluded groups
    assert "c-mortgage" in ids
    assert set(coord.store.low_state) == ids
    assert coord.store.low_state["c-gas"] is True  # balance 10 < 20% of target 200 (40)
    assert coord.low_share("c-gas") == 0.2
    coord.store.low_share_overrides["c-gas"] = 0.01
    assert coord.low_share("c-gas") == 0.01


async def test_sync_listener_called_and_removable(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    coord, _ = await _make(hass)
    calls = []
    remove = coord.add_sync_listener(lambda old, new: calls.append((old, new)))
    await _poll(coord)
    assert calls[0][0] is None and calls[0][1] is coord.data
    remove()
    await _poll(coord)
    assert len(calls) == 1


async def test_raising_listener_does_not_lose_delta(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    coord, _ = await _make(hass)

    def boom(old, new):
        raise RuntimeError("listener bug")

    coord.add_sync_listener(boom)
    await _poll(coord)
    assert coord.data is not None and coord._synced_once

    delta = {"data": {"transactions": [
        {**copy.deepcopy(fixture("transactions")["data"]["transactions"][0]), "id": "t-new"}
    ]}}
    _register(aioclient_mock, fixture, txs=delta, k=(1101, 1102, 1103))
    await _poll(coord)
    assert "t-new" in coord.data.transactions
    assert "t-mortgage" in coord.data.transactions  # earlier snapshot retained


async def test_network_error_keeps_knowledge(hass, aioclient_mock, fixture):
    import aiohttp

    _register(aioclient_mock, fixture)
    coord, _ = await _make(hass)
    await _poll(coord)
    before = dict(coord.store.knowledge)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{BASE}/accounts", exc=aiohttp.ClientError("down"))
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.store.knowledge == before


async def test_event_only_for_imported_transactions(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    events = async_capture_events(hass, EVENT_TRANSACTION_IMPORTED)
    coord, _ = await _make(hass)
    await _poll(coord)

    base = copy.deepcopy(fixture("transactions")["data"]["transactions"][0])
    delta = {"data": {"transactions": [
        {**base, "id": "t-manual", "import_id": None},
        {**base, "id": "t-bank", "import_id": "YNAB:-1:2026-11-03:1"},
    ]}}
    _register(aioclient_mock, fixture, txs=delta, k=(1101, 1102, 1103))
    await _poll(coord)
    await hass.async_block_till_done()

    assert [e.data["transaction_id"] for e in events] == ["t-bank"]
    assert "t-manual" in coord.data.transactions
    assert "t-manual" in coord.store.seen_transaction_ids
