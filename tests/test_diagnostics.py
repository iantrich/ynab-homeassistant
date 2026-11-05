"""Tests for diagnostics and entry lifecycle."""

import json

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ynab.diagnostics import async_get_config_entry_diagnostics
from tests.test_coordinator import _register

TOKEN = "super-secret-token-xyz"


async def _setup(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    entry = MockConfigEntry(
        domain="ynab",
        data={"api_token": TOKEN, "plan_id": "p", "currency": "USD"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_diagnostics_redacts_token(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert TOKEN not in json.dumps(diag)
    assert diag["entry"]["data"]["api_token"] == "**REDACTED**"
    assert diag["counts"]["accounts"] >= 1
    assert diag["requests_last_hour"] >= 1
    assert "knowledge" in diag and "store" in diag


async def test_unload_saves_store_and_remove_deletes(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    assert entry.state is ConfigEntryState.LOADED
    store = entry.runtime_data.store
    store.low_share_overrides["x"] = 0.4
    await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    from custom_components.ynab.store import YnabStore

    fresh = YnabStore(hass, entry.entry_id)
    await fresh.async_load()
    assert fresh.low_share_overrides == {"x": 0.4}
    await hass.config_entries.async_remove(entry.entry_id)
    again = YnabStore(hass, entry.entry_id)
    await again.async_load()
    assert again.low_share_overrides == {}
