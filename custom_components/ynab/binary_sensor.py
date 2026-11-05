"""Binary sensors: one "low" problem sensor per tracked category."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import YnabConfigEntry
from .coordinator import YnabCoordinator
from .entity import CONF_ENTITY_CATEGORIES, YnabEntity, async_add_new, selected_ids
from .models import Category

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: YnabConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create low sensors for categories with a target or a negative balance."""
    coordinator = entry.runtime_data.coordinator

    def build() -> dict[str, Any]:
        trackable = [
            c.id
            for c in coordinator.eligible_categories()
            if (c.goal_target is not None and c.goal_target > 0) or c.balance < 0
        ]
        return {
            f"low_{cid}": YnabLowSensor(coordinator, entry, cid)
            for cid in selected_ids(entry, CONF_ENTITY_CATEGORIES, trackable)
        }

    async_add_new(entry, coordinator, build, async_add_entities)


class YnabLowSensor(YnabEntity, BinarySensorEntity):
    """On while a category is below its low-share threshold or overspent."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key = "low"

    def __init__(self, coordinator: YnabCoordinator, entry: YnabConfigEntry, category_id: str) -> None:
        super().__init__(coordinator, entry)
        self._category_id = category_id
        self._attr_unique_id = f"{self._plan_id}_category_{category_id}_low"
        self._last_name = coordinator.data.categories[category_id].name

    @property
    def _category(self) -> Category | None:
        return self.coordinator.data.categories.get(self._category_id)

    @property
    def translation_placeholders(self) -> dict[str, str]:
        category = self._category
        if category is not None:
            self._last_name = category.name
        return {"category": self._last_name}

    @property
    def available(self) -> bool:
        return super().available and self._category is not None

    @property
    def is_on(self) -> bool | None:
        if self._category is None:
            return None
        return self.coordinator.store.low_state.get(self._category_id, False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cat = self._category
        if cat is None:
            return {}
        return {
            "category": cat.name,
            "available": float(cat.balance),
            "target": None if cat.goal_target is None else float(cat.goal_target),
            "share": self.coordinator.low_share(cat.id),
        }
