"""The YNAB integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import YnabClient
from .coordinator import YnabCoordinator
from .store import YnabStore

# Task 6 adds the sensor and binary_sensor platforms here.
PLATFORMS: list[Platform] = []


@dataclass
class YnabRuntime:
    """Objects that live as long as a loaded config entry."""

    client: YnabClient
    store: YnabStore
    coordinator: YnabCoordinator
    actions: Any | None = None  # YnabActions, added in Task 7


type YnabConfigEntry = ConfigEntry[YnabRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: YnabConfigEntry) -> bool:
    """Set up YNAB from a config entry."""
    client = YnabClient(async_get_clientsession(hass), entry.data["api_token"])
    store = YnabStore(hass, entry.entry_id)
    await store.async_load()

    coordinator = YnabCoordinator(
        hass,
        entry,
        client,
        store,
        entry.data["plan_id"],
        entry.data.get("currency", "USD"),
    )
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = YnabRuntime(
        client=client, store=store, coordinator=coordinator, actions=None
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Reload on options changes so scan_minutes and entity selections apply.
    loaded_options = dict(entry.options)

    async def _async_update_listener(
        hass: HomeAssistant, updated: YnabConfigEntry
    ) -> None:
        # Data-only updates (reauth) reload themselves; avoid a second reload.
        if dict(updated.options) != loaded_options:
            await hass.config_entries.async_reload(updated.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: YnabConfigEntry) -> bool:
    """Unload a config entry, saving state immediately."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.store.async_save_now()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: YnabConfigEntry) -> None:
    """Delete the persisted store when the entry is removed."""
    await YnabStore(hass, entry.entry_id).async_remove()
