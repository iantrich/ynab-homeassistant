"""The YNAB integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .actions import YnabActions
from .client import YnabClient
from .const import DOMAIN, EVENT_RECURRING_DETECTED
from .coordinator import YnabCoordinator, YnabData
from .recurring import detect_recurring
from .services import async_register_services
from .store import YnabStore

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


@dataclass
class YnabRuntime:
    """Objects that live as long as a loaded config entry."""

    client: YnabClient
    store: YnabStore
    coordinator: YnabCoordinator
    actions: YnabActions | None = None


type YnabConfigEntry = ConfigEntry[YnabRuntime]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services once for the whole integration."""
    async_register_services(hass)
    return True


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

    actions = YnabActions(hass, entry, client, coordinator, store)
    entry.runtime_data = YnabRuntime(
        client=client, store=store, coordinator=coordinator, actions=actions
    )
    entry.async_on_unload(coordinator.add_sync_listener(actions.on_synced))
    # The first refresh ran before the listener existed; catch up on restored proposals.
    actions.on_synced(None, coordinator.data)

    def _report_recurring(old: YnabData | None, new: YnabData) -> None:
        today = dt_util.now().date()
        fresh = [
            r
            for r in detect_recurring(new.transactions.values(), today)
            if r.key not in store.recurring_reported
        ]
        for rec in fresh:
            store.recurring_reported.add(rec.key)
            hass.bus.async_fire(
                EVENT_RECURRING_DETECTED,
                {**rec.as_event_data(), "entry_id": entry.entry_id},
            )
        if fresh:
            store.schedule_save()

    entry.async_on_unload(coordinator.add_sync_listener(_report_recurring))
    _report_recurring(None, coordinator.data)
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
