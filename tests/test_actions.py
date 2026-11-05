"""Tests for write actions and services."""

import copy
from decimal import Decimal

import pytest
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from tests.conftest import BASE, _register


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")


async def _setup(hass, aioclient_mock, fixture, categories=None):
    _register(aioclient_mock, fixture, categories=categories)
    entry = MockConfigEntry(
        domain="ynab",
        title="Household",
        data={"api_token": "tok", "plan_id": "p", "plan_name": "Household", "currency": "USD"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    aioclient_mock.patch(f"{BASE}/transactions", json={"data": {"transactions": []}})
    aioclient_mock.patch(
        f"{BASE}/months/current/categories/c-gas", json={"data": {"category": {}}}
    )
    return entry


def _writes(aioclient_mock):
    return [c for c in aioclient_mock.mock_calls if c[0] in ("PATCH", "PUT", "POST")]


async def test_update_sets_category_and_approves(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    events = async_capture_events(hass, "ynab_transaction_updated")
    ids = await entry.runtime_data.actions.update_transactions(
        [{"transaction_id": "t-withdrawal", "category": "groceries", "approve": True}],
        source="user:abc",
    )
    assert ids == ["t-withdrawal"]
    writes = _writes(aioclient_mock)
    assert len(writes) == 1
    assert writes[0][2] == {
        "transactions": [
            {"id": "t-withdrawal", "category_id": "c-groceries", "approved": True}
        ]
    }
    await hass.async_block_till_done()
    assert len(events) == 1
    assert events[0].data == {
        "entry_id": entry.entry_id,
        "transaction_id": "t-withdrawal",
        "changes": {
            "category_id": "c-groceries",
            "category_name": "Groceries",
            "approved": True,
        },
        "source": "user:abc",
    }
    last = entry.runtime_data.store.activity[-1]
    assert last["kind"] == "transaction_updated"
    assert last["source"] == "user:abc"
    assert last["transaction_id"] == "t-withdrawal"
    assert "tok" not in str(events[0].data)


async def test_payee_and_memo_use_payee_name(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    await entry.runtime_data.actions.update_transactions(
        [{"transaction_id": "t-deposit", "payee_name": "Costco", "memo": "bulk"}],
        source="llm",
    )
    assert _writes(aioclient_mock)[0][2]["transactions"][0] == {
        "id": "t-deposit", "payee_name": "Costco", "memo": "bulk"
    }


async def test_refuses_approved_without_force(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    with pytest.raises(ServiceValidationError, match="already approved"):
        await entry.runtime_data.actions.update_transactions(
            [{"transaction_id": "t-mortgage", "memo": "x"}], source="llm"
        )
    assert _writes(aioclient_mock) == []


async def test_force_allows_approved(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    await entry.runtime_data.actions.update_transactions(
        [{"transaction_id": "t-mortgage", "memo": "x", "force": True}], source="llm"
    )
    assert _writes(aioclient_mock)[0][2]["transactions"] == [{"id": "t-mortgage", "memo": "x"}]


@pytest.mark.parametrize("tx_id", ["t-split", "t-transfer"])
async def test_refuses_category_on_split_or_transfer(hass, aioclient_mock, fixture, tx_id):
    entry = await _setup(hass, aioclient_mock, fixture)
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.actions.update_transactions(
            [{"transaction_id": tx_id, "category": "Groceries", "force": True}],
            source="llm",
        )
    assert _writes(aioclient_mock) == []


async def test_refuses_unknown_and_deleted_ids_and_empty_change(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    for update in (
        {"transaction_id": "nope", "memo": "x"},
        {"transaction_id": "t-deleted", "memo": "x"},
        {"transaction_id": "t-deposit"},
    ):
        with pytest.raises(ServiceValidationError):
            await actions.update_transactions([update], source="llm")
    assert _writes(aioclient_mock) == []


async def test_batch_is_all_or_nothing(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    with pytest.raises(ServiceValidationError):
        await entry.runtime_data.actions.update_transactions(
            [
                {"transaction_id": "t-deposit", "memo": "ok"},
                {"transaction_id": "t-mortgage", "memo": "approved, no force"},
            ],
            source="llm",
        )
    assert _writes(aioclient_mock) == []


async def test_validate_update_does_no_io(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    before = len(aioclient_mock.mock_calls)
    tx, patch = entry.runtime_data.actions._validate_update(
        {"transaction_id": "t-deposit", "category": "c-gas"}
    )
    assert tx.id == "t-deposit"
    assert patch == {"id": "t-deposit", "category_id": "c-gas"}
    assert len(aioclient_mock.mock_calls) == before


async def test_resolve_category_ambiguous_lists_candidates(hass, aioclient_mock, fixture):
    cats = copy.deepcopy(fixture("categories"))
    fun = next(g for g in cats["data"]["category_groups"] if g["name"] == "Fun")
    dup = copy.deepcopy(next(
        c for g in cats["data"]["category_groups"] for c in g["categories"] if c["id"] == "c-groceries"
    ))
    dup["id"] = "c-groceries-2"
    dup["category_group_id"] = "g-fun"
    dup["category_group_name"] = "Fun"
    fun["categories"].append(dup)
    entry = await _setup(hass, aioclient_mock, fixture, categories=cats)
    actions = entry.runtime_data.actions
    with pytest.raises(ServiceValidationError) as err:
        actions.resolve_category("groceries")
    assert "c-groceries" in str(err.value) and "c-groceries-2" in str(err.value)
    assert actions.resolve_category("c-groceries-2").group_name == "Fun"
    with pytest.raises(ServiceValidationError, match="Unknown"):
        actions.resolve_category("Chase Visa")  # excluded group
    with pytest.raises(ServiceValidationError, match="Unknown"):
        actions.resolve_category("Old Stuff (hidden)")


async def test_assign_money_add_mode(hass, aioclient_mock, fixture):
    cats = copy.deepcopy(fixture("categories"))
    for g in cats["data"]["category_groups"]:
        for c in g["categories"]:
            if c["id"] == "c-gas":
                c["budgeted"] = 50000
    entry = await _setup(hass, aioclient_mock, fixture, categories=cats)
    new = await entry.runtime_data.actions.assign_money(
        "Gas", Decimal("25"), mode="add", source="user:abc"
    )
    assert new == Decimal("75")
    call = _writes(aioclient_mock)[0]
    assert call[0] == "PATCH"
    assert str(call[1]).endswith("/months/current/categories/c-gas")
    assert call[2] == {"category": {"budgeted": 75000}}
    assert entry.runtime_data.store.activity[-1]["kind"] == "money_assigned"


async def test_assign_money_set_and_validation(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    actions = entry.runtime_data.actions
    assert await actions.assign_money("c-gas", Decimal("10.5"), source="llm") == Decimal("10.5")
    assert _writes(aioclient_mock)[0][2] == {"category": {"budgeted": 10500}}
    for kwargs in (
        {"amount": Decimal("-1")},
        {"amount": Decimal("-500"), "mode": "add"},
        {"amount": Decimal("1"), "month": "2026-11"},
        {"amount": Decimal("1"), "month": "2026-13-01"},
    ):
        with pytest.raises(ServiceValidationError):
            await actions.assign_money("c-gas", source="llm", **kwargs)
    assert len(_writes(aioclient_mock)) == 1


async def test_assign_money_other_month_reads_that_month(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    month = copy.deepcopy(fixture("month"))
    month["data"]["month"]["month"] = "2026-12-01"
    aioclient_mock.get(f"{BASE}/months/2026-12-01", json=month)
    aioclient_mock.patch(
        f"{BASE}/months/2026-12-01/categories/c-gas", json={"data": {"category": {}}}
    )
    # c-gas is 200 in the fixture month; add 5 -> 205
    new = await entry.runtime_data.actions.assign_money(
        "c-gas", Decimal("5"), month="2026-12-01", mode="add", source="llm"
    )
    assert new == Decimal("205")
    call = _writes(aioclient_mock)[0]
    assert str(call[1]).endswith("/months/2026-12-01/categories/c-gas")
    assert call[2] == {"category": {"budgeted": 205000}}


async def test_service_call_end_to_end(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    events = async_capture_events(hass, "ynab_transaction_updated")
    result = await hass.services.async_call(
        "ynab",
        "update_transaction",
        {"config_entry_id": entry.entry_id, "transaction_id": "t-deposit",
         "category": "Groceries", "approve": True},
        blocking=True,
        return_response=True,
    )
    assert result == {"updated": ["t-deposit"]}
    assert _writes(aioclient_mock)[0][2]["transactions"][0]["category_id"] == "c-groceries"
    await hass.async_block_till_done()
    assert events[0].data["source"] == "service"

    result = await hass.services.async_call(
        "ynab", "assign_money",
        {"config_entry_id": entry.entry_id, "category": "Gas", "amount": 12.5},
        blocking=True, return_response=True,
    )
    assert result == {"assigned": 12.5}


async def test_service_rejects_bad_entry(hass, aioclient_mock, fixture):
    await _setup(hass, aioclient_mock, fixture)
    other = MockConfigEntry(domain="other")
    other.add_to_hass(hass)
    for entry_id in ("missing", other.entry_id):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "ynab", "refresh", {"config_entry_id": entry_id}, blocking=True
            )
