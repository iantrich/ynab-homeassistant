"""Tests for the config and options flows."""

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ynab.const import API_BASE, DOMAIN, PLAN_SEGMENT

PLAN_ID = "11111111-1111-4111-8111-111111111111"
BASE = f"{API_BASE}/{PLAN_SEGMENT}/{PLAN_ID}"
TOKEN = "secret-token-123"


@pytest.fixture(autouse=True)
def no_setup(monkeypatch):
    """Keep flow tests from running a full entry setup."""
    async def _ok(hass, entry):
        return True

    monkeypatch.setattr("custom_components.ynab.async_setup_entry", _ok)


def _mock_api(mock, fixture):
    mock.get(f"{API_BASE}/user", json=fixture("user"))
    mock.get(f"{API_BASE}/{PLAN_SEGMENT}", json=fixture("plans"))


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_happy_path(hass, aioclient_mock, fixture):
    _mock_api(aioclient_mock, fixture)
    result = await _start(hass)
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_token": TOKEN}
    )
    assert result["step_id"] == "plan"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"plan_id": PLAN_ID}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Richardson Family Budget"
    assert result["data"] == {
        "api_token": TOKEN,
        "plan_id": PLAN_ID,
        "plan_name": "Richardson Family Budget",
        "currency": "USD",
    }
    assert result["result"].unique_id == PLAN_ID


async def test_invalid_auth(hass, aioclient_mock):
    aioclient_mock.get(
        f"{API_BASE}/user", status=401, json={"error": {"detail": "Unauthorized"}}
    )
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_token": TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get(f"{API_BASE}/user", status=500)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_token": TOKEN}
    )
    assert result["errors"] == {"base": "cannot_connect"}


async def test_duplicate_plan_aborts(hass, aioclient_mock, fixture):
    MockConfigEntry(
        domain=DOMAIN, unique_id=PLAN_ID, data={"api_token": "x", "plan_id": PLAN_ID}
    ).add_to_hass(hass)
    _mock_api(aioclient_mock, fixture)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_token": TOKEN}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"plan_id": PLAN_ID}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_token(hass, aioclient_mock, fixture):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PLAN_ID,
        title="Plan",
        data={"api_token": "old", "plan_id": PLAN_ID},
    )
    entry.add_to_hass(hass)
    _mock_api(aioclient_mock, fixture)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_token": "new-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["api_token"] == "new-token"
    assert entry.data["plan_id"] == PLAN_ID


async def test_reauth_wrong_account(hass, aioclient_mock, fixture):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="other", data={"api_token": "old", "plan_id": "other"}
    )
    entry.add_to_hass(hass)
    _mock_api(aioclient_mock, fixture)
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"api_token": "new-token"}
    )
    assert result["errors"] == {"base": "wrong_account"}
    assert entry.data["api_token"] == "old"


# -- options flow -----------------------------------------------------------------


@pytest.fixture
async def loaded(hass, aioclient_mock, fixture, monkeypatch):
    """A fully set-up entry (real setup, not the no-op patch)."""
    monkeypatch.undo()
    from tests.test_coordinator import _register

    monkeypatch.setattr("custom_components.ynab.coordinator.WINDOW_DAYS", 100000)
    _register(aioclient_mock, fixture)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PLAN_ID,
        data={"api_token": TOKEN, "plan_id": "p", "currency": "USD"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_options_general(hass, loaded):
    cat_id = loaded.runtime_data.coordinator.eligible_categories()[0].id
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "general"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_minutes": 30,
            "low_share": 0.3,
            "entity_accounts": [],
            "entity_categories": [cat_id],
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert loaded.options["scan_minutes"] == 30
    assert loaded.options["low_share"] == 0.3
    assert loaded.options["entity_categories"] == [cat_id]


async def test_options_scan_minutes_out_of_range(hass, loaded):
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "general"}
    )
    with pytest.raises(Exception):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"scan_minutes": 2, "low_share": 0.3}
        )


async def test_options_stages(hass, loaded):
    store = loaded.runtime_data.store
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "stages"}
    )
    schema_keys = [str(k) for k in result["data_schema"].schema]
    assert "Bills" in schema_keys
    assert "Credit Card Payments" not in schema_keys
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"Bills": "bills", "Everyday": "everyday"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert store.stage_mapping["groups"]["g-bills"] == "bills"
    assert store.stage_mapping["groups"]["g-everyday"] == "everyday"


async def test_options_low_overrides(hass, loaded):
    store = loaded.runtime_data.store
    cat = next(iter(loaded.runtime_data.coordinator.eligible_categories()))
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "low_overrides"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"category_id": cat.id, "share": 0.5}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert store.low_share_overrides[cat.id] == 0.5

    # Clearing: omit share.
    result = await hass.config_entries.options.async_init(loaded.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "low_overrides"}
    )
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"category_id": cat.id}
    )
    assert cat.id not in store.low_share_overrides
