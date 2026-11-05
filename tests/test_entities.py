"""Tests for sensors and low-bucket binary sensors."""

import copy

import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import _register


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")


async def _setup(hass, aioclient_mock, fixture, options=None):
    _register(aioclient_mock, fixture)
    entry = MockConfigEntry(
        domain="ynab",
        title="Household",
        data={"api_token": "tok", "plan_id": "p", "plan_name": "Household", "currency": "USD"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _poll(hass, entry):
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def _unique_ids(hass, entry):
    return {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)}


async def test_account_sensor(hass, aioclient_mock, fixture):
    await _setup(hass, aioclient_mock, fixture)
    state = hass.states.get("sensor.household_apple_savings")
    assert state.state == "12500"
    assert state.attributes["unit_of_measurement"] == "USD"
    assert state.attributes["device_class"] == "monetary"
    assert state.attributes["type"] == "savings"
    assert state.attributes["on_budget"] is True
    assert state.attributes["cleared_balance"] == 12500.0
    assert state.attributes["uncleared_balance"] == 0.0


async def test_category_sensor_attributes_and_stage(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    entry.runtime_data.store.stage_mapping["groups"]["g-everyday"] = "everyday"
    entry.runtime_data.store.stage_mapping["categories"]["c-gas"] = "bills"
    await _poll(hass, entry)
    state = hass.states.get("sensor.household_groceries")
    assert state.state == "150"
    assert state.attributes["assigned"] == 800.0
    assert state.attributes["activity"] == -650.0
    assert state.attributes["target"] == 800.0
    assert state.attributes["group"] == "Everyday"
    assert state.attributes["stage"] == "everyday"
    assert state.attributes["underfunded"] is None
    assert hass.states.get("sensor.household_gas").attributes["stage"] == "bills"
    assert hass.states.get("sensor.household_mortgage").attributes["stage"] is None


async def test_summary_sensors(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    assert hass.states.get("sensor.household_ready_to_assign").state == "1250"
    coord = entry.runtime_data.coordinator
    expected = sum(1 for t in coord.data.transactions.values() if not t.approved)
    assert int(hass.states.get("sensor.household_unapproved_transactions").state) == expected
    assert hass.states.get("sensor.household_pending_proposals").state == "0"
    entry.runtime_data.store.proposals["x"] = {"status": "pending"}
    entry.runtime_data.store.proposals["y"] = {"status": "applied"}
    coord.async_update_listeners()
    assert hass.states.get("sensor.household_pending_proposals").state == "1"


async def test_excluded_categories_and_closed_accounts_have_no_entities(
    hass, aioclient_mock, fixture
):
    entry = await _setup(hass, aioclient_mock, fixture)
    ids = _unique_ids(hass, entry)
    assert "p_category_c-oldstuff" not in ids  # hidden
    assert "p_category_c-ccpay" not in ids  # Credit Card Payments
    assert "p_category_c-rta" not in ids
    assert "p_account_acc-old" not in ids
    assert "p_category_c-groceries" in ids
    assert {"p_ready_to_assign", "p_unapproved", "p_proposals"} <= ids


async def test_devices(hass, aioclient_mock, fixture):
    from homeassistant.helpers import device_registry as dr

    entry = await _setup(hass, aioclient_mock, fixture)
    reg = dr.async_get(hass)
    plan = reg.async_get_device_by_identifier(("ynab", "p"), entry.entry_id)
    acct = reg.async_get_device_by_identifier(("ynab", "p_acc-savings"), entry.entry_id)
    assert plan.name == "Household"
    assert acct.via_device_id == plan.id


async def test_selection_options(hass, aioclient_mock, fixture):
    entry = await _setup(
        hass,
        aioclient_mock,
        fixture,
        {"entity_accounts": ["acc-savings"], "entity_categories": ["c-groceries"]},
    )
    ids = _unique_ids(hass, entry)
    assert "p_account_acc-savings" in ids and "p_account_acc-checking" not in ids
    assert "p_category_c-groceries" in ids and "p_category_c-gas" not in ids
    assert "p_category_c-groceries_low" in ids and "p_category_c-gas_low" not in ids


async def test_empty_selection_means_none(hass, aioclient_mock, fixture):
    entry = await _setup(
        hass, aioclient_mock, fixture, {"entity_accounts": [], "entity_categories": []}
    )
    ids = _unique_ids(hass, entry)
    assert not any("_account_" in i or "_category_" in i for i in ids)


async def test_low_binary_sensor_transitions(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    entity_id = "binary_sensor.household_utilities_low"
    state = hass.states.get(entity_id)
    assert state.state == "off"
    assert state.attributes["device_class"] == "problem"

    cats = fixture("categories")
    for g in cats["data"]["category_groups"]:
        for c in g["categories"]:
            if c["id"] == "c-utilities":
                c["balance"] = 40000  # 13% of 300 < 20%
    _register(aioclient_mock, fixture, categories=cats, k=(1001, 1004, 1003))
    await _poll(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.attributes["category"] == "Utilities"
    assert state.attributes["available"] == 40.0
    assert state.attributes["target"] == 300.0
    assert state.attributes["share"] == 0.2
    changed = state.last_changed

    await _poll(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert state.last_changed == changed


async def test_new_category_in_delta_gets_entities(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    assert hass.states.get("sensor.household_vacation") is None
    cats = fixture("categories")
    new = copy.deepcopy(cats["data"]["category_groups"][2]["categories"][0])
    new.update(id="c-vacation", name="Vacation", balance=50000, goal_target=1000000)
    cats["data"]["category_groups"][2]["categories"].append(new)
    _register(aioclient_mock, fixture, categories=cats, k=(1001, 1005, 1003))
    await _poll(hass, entry)
    assert hass.states.get("sensor.household_vacation").state == "50"
    assert hass.states.get("binary_sensor.household_vacation_low").state == "on"


async def test_deleted_category_becomes_unavailable(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    cats = fixture("categories")
    for g in cats["data"]["category_groups"]:
        for c in g["categories"]:
            if c["id"] == "c-gas":
                c["deleted"] = True
    _register(aioclient_mock, fixture, categories=cats, k=(1001, 1006, 1003))
    await _poll(hass, entry)
    assert hass.states.get("sensor.household_gas").state == "unavailable"
    assert hass.states.get("binary_sensor.household_gas_low").state == "unavailable"
