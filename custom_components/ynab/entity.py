"""Shared entity helpers for the YNAB integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import YnabCoordinator

if TYPE_CHECKING:
    from . import YnabConfigEntry

CONF_ENTITY_ACCOUNTS = "entity_accounts"
CONF_ENTITY_CATEGORIES = "entity_categories"


def ensure_plan_device(
    hass: HomeAssistant, entry: YnabConfigEntry, coordinator: YnabCoordinator
) -> str:
    """Create (or fetch) the plan device and return its registry id."""
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, coordinator.plan_id)},
        name=entry.data.get("plan_name") or entry.title,
        manufacturer="YNAB",
    ).id


def selected_ids(entry: YnabConfigEntry, option: str, available: Iterable[str]) -> set[str]:
    """Ids that should have entities.

    No saved selection means "everything available" (so new items are added
    automatically). A saved selection is exact, and an empty one means none.
    """
    available = set(available)
    if option not in entry.options:
        return available
    return set(entry.options[option]) & available


def async_add_new(
    entry: YnabConfigEntry,
    coordinator: YnabCoordinator,
    build: Callable[[], dict[str, Entity]],
    async_add_entities: Callable[[list[Entity]], None],
) -> None:
    """Add entities now and for anything that appears on later polls.

    ``build`` returns ``{unique_key: entity}`` for everything that should
    exist right now; keys already added are skipped.
    """
    known: set[str] = set()

    def _sync() -> None:
        wanted = build()
        fresh = [ent for key, ent in wanted.items() if key not in known]
        if fresh:
            known.update(k for k in wanted if k not in known)
            async_add_entities(fresh)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


class YnabEntity(CoordinatorEntity[YnabCoordinator]):
    """Base class: every entity belongs to a plan (or account) device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: YnabCoordinator, entry: YnabConfigEntry) -> None:
        super().__init__(coordinator)
        self._plan_id = coordinator.plan_id
        self._plan_name = entry.data.get("plan_name") or entry.title
        self._attr_device_info = self._plan_device()

    def _plan_device(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._plan_id)},
            name=self._plan_name,
            manufacturer="YNAB",
        )
