"""Tests for transaction proposals."""

import copy
import dataclasses
from datetime import timedelta

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from tests.conftest import BASE, _register


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")


async def _setup(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    entry = MockConfigEntry(
        domain="ynab",
        title="Household",
        data={"api_token": "tok", "plan_id": "p", "plan_name": "Household", "currency": "USD"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.patch(f"{BASE}/transactions", json={"data": {"transactions": []}})
    return entry


def _writes(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if c[0] in ("PATCH", "PUT", "POST")]


def _approve_in_app(aioclient_mock, fixture, tx_id, **changes):
    """Register a transactions delta in which the user edited tx_id in YNAB."""
    txs = copy.deepcopy(fixture("transactions"))
    txs["data"]["server_knowledge"] = 1200
    txs["data"]["transactions"] = [
        {**t, "approved": True, **changes}
        for t in txs["data"]["transactions"]
        if t["id"] == tx_id
    ]
    aioclient_mock.clear_requests()  # first registered match wins
    aioclient_mock.get(f"{BASE}/transactions", json=txs)
    aioclient_mock.patch(f"{BASE}/transactions", json={"data": {"transactions": []}})


async def _resync(hass, aioclient_mock, fixture, mutate):
    """Poll again with a modified transactions payload."""
    txs = copy.deepcopy(fixture("transactions"))
    mutate(txs["data"]["transactions"])
    _register(aioclient_mock, fixture, txs=txs, k=(1101, 1102, 1103))
    aioclient_mock.patch(f"{BASE}/transactions", json={"data": {"transactions": []}})
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()


def _pending_state(hass):
    return next(
        s for s in hass.states.async_all("sensor") if s.entity_id.endswith("proposals")
    )


async def test_propose_stores_without_writing_and_fires_event(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    events = async_capture_events(hass, "ynab_proposal_created")
    actions = entry.runtime_data.actions
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "category": "groceries", "approve": True},
        source="automation:x",
    )
    await hass.async_block_till_done()
    assert _writes(aioclient_mock) == []
    assert pid.startswith("p-") and len(pid) == 8
    p = entry.runtime_data.store.proposals[pid]
    assert p["status"] == "pending"
    assert p["source"] == "automation:x"
    assert p["changes"] == {
        "category_id": "c-groceries",
        "category_name": "Groceries",
        "approve": True,
    }
    assert p["snapshot"] == {
        "date": "2026-11-03",
        "amount": -623.98,
        "account_name": "Apple Savings",
        "import_payee_name": None,
        "category_id": None,
    }
    assert len(events) == 1
    assert events[0].data["proposal_id"] == pid
    assert events[0].data["kind"] == "transaction"
    assert events[0].data["entry_id"] == entry.entry_id
    assert "Groceries" in events[0].data["summary"]
    assert _pending_state(hass).state == "1"


async def test_second_proposal_keeps_id(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    first = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "category": "groceries"}, source="a"
    )
    second = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "memo": "new"}, source="b"
    )
    assert first == second
    assert len(entry.runtime_data.store.proposals) == 1
    assert entry.runtime_data.store.proposals[first]["changes"] == {"memo": "new"}
    assert entry.runtime_data.store.proposals[first]["source"] == "b"


async def test_propose_validates(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    with pytest.raises(ServiceValidationError):
        await actions.propose_transaction_update(
            {"transaction_id": "nope", "memo": "x"}, source="a"
        )
    with pytest.raises(ServiceValidationError):  # already approved; force ignored
        await actions.propose_transaction_update(
            {"transaction_id": "t-mortgage", "memo": "x", "force": True}, source="a"
        )
    assert entry.runtime_data.store.proposals == {}


async def test_approve_applies_in_one_call(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    p1 = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "category": "groceries"}, source="a"
    )
    p2 = await actions.propose_transaction_update(
        {"transaction_id": "t-deposit", "memo": "m", "approve": False}, source="a"
    )
    result = await actions.approve_proposals([p1, p2, "p-zzzzzz"], source="user:u")
    await hass.async_block_till_done()
    assert result == {"applied": [p1, p2], "skipped": {"p-zzzzzz": "unknown"}}
    writes = _writes(aioclient_mock)
    assert len(writes) == 1
    assert writes[0][2] == {
        "transactions": [
            {"id": "t-withdrawal", "category_id": "c-groceries", "approved": True},
            {"id": "t-deposit", "memo": "m", "approved": False},
        ]
    }
    store = entry.runtime_data.store
    assert store.proposals[p1]["status"] == "approved"
    assert store.proposals[p2]["status"] == "approved"
    assert _pending_state(hass).state == "0"
    again = await actions.approve_proposals([p1], source="user:u")
    assert again == {"applied": [], "skipped": {p1: "approved"}}


async def test_approve_failure_leaves_pending(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "memo": "x"}, source="a"
    )
    _register(aioclient_mock, fixture)
    aioclient_mock.patch(f"{BASE}/transactions", status=400, json={"error": {"detail": "no"}})
    with pytest.raises(HomeAssistantError):
        await actions.approve_proposals([pid], source="u")
    assert entry.runtime_data.store.proposals[pid]["status"] == "pending"


async def test_reject(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "memo": "x"}, source="a"
    )
    result = await actions.reject_proposals([pid, "p-nope"], source="u")
    assert result == {"rejected": [pid], "skipped": {"p-nope": "unknown"}}
    assert entry.runtime_data.store.proposals[pid]["status"] == "rejected"
    assert _writes(aioclient_mock) == []
    assert _pending_state(hass).state == "0"
    assert actions.list_proposals() == []
    assert [p["id"] for p in actions.list_proposals("rejected")] == [pid]
    assert [p["id"] for p in actions.list_proposals("all")] == [pid]


async def test_approved_elsewhere_makes_stale(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "category": "groceries"}, source="a"
    )

    def mutate(txs):
        for t in txs:
            if t["id"] == "t-withdrawal":
                t["approved"] = True

    await _resync(hass, aioclient_mock, fixture, mutate)
    assert entry.runtime_data.store.proposals[pid]["status"] == "stale"
    assert _pending_state(hass).state == "0"
    result = await actions.approve_proposals([pid], source="u")
    assert result == {"applied": [], "skipped": {pid: "stale"}}
    assert _writes(aioclient_mock) == []


async def test_deleted_transaction_makes_stale(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-deposit", "memo": "x"}, source="a"
    )

    def mutate(txs):
        for t in txs:
            if t["id"] == "t-deposit":
                t["deleted"] = True

    await _resync(hass, aioclient_mock, fixture, mutate)
    assert entry.runtime_data.store.proposals[pid]["status"] == "stale"


async def test_category_change_makes_stale(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "memo": "x"}, source="a"
    )

    def mutate(txs):
        for t in txs:
            if t["id"] == "t-withdrawal":
                t["category_id"] = "c-groceries"

    await _resync(hass, aioclient_mock, fixture, mutate)
    assert entry.runtime_data.store.proposals[pid]["status"] == "stale"


async def test_approve_revalidates_and_skips_unsupported(
    hass, aioclient_mock, fixture, freezer
):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    store = entry.runtime_data.store
    store.proposals["p-alloc1"] = {
        "id": "p-alloc1", "kind": "allocation", "status": "pending",
        "created_at": dt_util.utcnow().isoformat(),
    }
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "memo": "x"}, source="a"
    )
    # Approved in the YNAB app since the last poll: approve_proposals pulls
    # the transactions delta first, so it sees this and skips as stale.
    freezer.tick(31)
    _approve_in_app(aioclient_mock, fixture, "t-withdrawal")
    result = await actions.approve_proposals([pid, "p-alloc1"], source="u")
    assert result["applied"] == []
    assert result["skipped"] == {pid: "stale", "p-alloc1": "unsupported_kind"}
    assert _writes(aioclient_mock) == []


async def test_old_resolved_proposals_pruned(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    store = entry.runtime_data.store
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "memo": "x"}, source="a"
    )
    await actions.reject_proposals([pid], source="u")
    store.proposals[pid]["resolved_at"] = (
        dt_util.utcnow() - timedelta(days=31)
    ).isoformat()
    await _resync(hass, aioclient_mock, fixture, lambda txs: None)
    assert pid not in store.proposals


async def test_services(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    resp = await hass.services.async_call(
        "ynab",
        "propose_transaction_update",
        {"config_entry_id": entry.entry_id, "transaction_id": "t-withdrawal", "category": "Groceries"},
        blocking=True,
        return_response=True,
    )
    pid = resp["proposal_id"]
    listed = await hass.services.async_call(
        "ynab", "list_proposals", {"config_entry_id": entry.entry_id},
        blocking=True, return_response=True,
    )
    assert [p["id"] for p in listed["proposals"]] == [pid]
    resp = await hass.services.async_call(
        "ynab", "approve_proposals",
        {"config_entry_id": entry.entry_id, "proposal_ids": [pid]},
        blocking=True, return_response=True,
    )
    assert resp == {"applied": [pid], "skipped": {}}
    resp = await hass.services.async_call(
        "ynab", "reject_proposals",
        {"config_entry_id": entry.entry_id, "proposal_ids": [pid]},
        blocking=True, return_response=True,
    )
    assert resp == {"rejected": [], "skipped": {pid: "approved"}}


async def test_approve_refreshes_transactions_first(hass, aioclient_mock, fixture, freezer):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    coordinator = entry.runtime_data.coordinator
    pid = await actions.propose_transaction_update(
        {"transaction_id": "t-withdrawal", "category": "Groceries"}, source="a"
    )
    freezer.tick(31)
    _approve_in_app(aioclient_mock, fixture, "t-withdrawal")
    aioclient_mock.mock_calls.clear()
    result = await actions.approve_proposals([pid], source="u")
    assert result == {"applied": [], "skipped": {pid: "stale"}}
    assert _writes(aioclient_mock) == []
    gets = [c for c in aioclient_mock.mock_calls if c[0] == "GET"]
    assert len(gets) == 1 and str(gets[0][1]).startswith(f"{BASE}/transactions")
    assert gets[0][1].query == {"last_knowledge_of_server": "1003"}
    assert coordinator.store.knowledge["transactions"] == 1200
    assert coordinator.store.knowledge["accounts"] == 1001  # only transactions advance
    assert entry.runtime_data.store.proposals[pid]["status"] == "stale"


async def test_recent_fetch_skips_refresh(hass, aioclient_mock, fixture, freezer):
    entry = await _setup(hass, aioclient_mock, fixture)
    coordinator = entry.runtime_data.coordinator
    freezer.tick(10)  # < 30 s since the setup poll
    aioclient_mock.mock_calls.clear()
    await coordinator.async_refresh_transactions()
    assert aioclient_mock.mock_calls == []
    freezer.tick(21)
    await coordinator.async_refresh_transactions()
    assert len(aioclient_mock.mock_calls) == 1
