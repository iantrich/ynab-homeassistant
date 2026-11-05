"""YNAB sensors: accounts, categories, Ready to Assign and counters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import YnabConfigEntry
from .const import DOMAIN
from .coordinator import YnabCoordinator
from .entity import (
    CONF_ENTITY_ACCOUNTS,
    CONF_ENTITY_CATEGORIES,
    YnabEntity,
    async_add_new,
    ensure_plan_device,
    selected_ids,
)
from .models import Account, Category

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: YnabConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Create sensors, and more whenever new accounts/categories show up."""
    coordinator = entry.runtime_data.coordinator
    plan_device_id = ensure_plan_device(hass, entry, coordinator)

    def build() -> dict[str, Any]:
        data = coordinator.data
        wanted: dict[str, Any] = {}
        for key, desc in (
            ("ready_to_assign", READY_TO_ASSIGN),
            ("unapproved", UNAPPROVED),
            ("proposals", PROPOSALS),
        ):
            wanted[key] = YnabSummarySensor(coordinator, entry, desc)
        eligible_accounts = [a.id for a in data.accounts.values() if not a.closed]
        for aid in selected_ids(entry, CONF_ENTITY_ACCOUNTS, eligible_accounts):
            wanted[f"account_{aid}"] = YnabAccountSensor(coordinator, entry, aid, plan_device_id)
        eligible = [c.id for c in coordinator.eligible_categories()]
        for cid in selected_ids(entry, CONF_ENTITY_CATEGORIES, eligible):
            wanted[f"category_{cid}"] = YnabCategorySensor(coordinator, entry, cid)
        return wanted

    async_add_new(entry, coordinator, build, async_add_entities)


class YnabAccountSensor(YnabEntity, SensorEntity):
    """Balance of one account."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_name = None

    def __init__(self, coordinator: YnabCoordinator, entry: YnabConfigEntry, account_id: str, plan_device_id: str
    ) -> None:
        super().__init__(coordinator, entry)
        self._account_id = account_id
        self._attr_unique_id = f"{self._plan_id}_account_{account_id}"
        self._attr_native_unit_of_measurement = coordinator.currency
        account = coordinator.data.accounts[account_id]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{self._plan_id}_{account_id}")},
            # Prefixed with the plan so two plans can't collide on entity ids.
            name=f"{self._plan_name} {account.name}",
            manufacturer="YNAB",
            model=account.type,
            via_device_id=plan_device_id,
        )

    @property
    def _account(self) -> Account | None:
        return self.coordinator.data.accounts.get(self._account_id)

    @property
    def available(self) -> bool:
        return super().available and self._account is not None

    @property
    def native_value(self) -> Decimal | None:
        account = self._account
        return account.balance if account else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        account = self._account
        if account is None:
            return {}
        return {
            "cleared_balance": float(account.cleared_balance),
            "uncleared_balance": float(account.uncleared_balance),
            "type": account.type,
            "on_budget": account.on_budget,
        }


class YnabCategorySensor(YnabEntity, SensorEntity):
    """Available balance of one category."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: YnabCoordinator, entry: YnabConfigEntry, category_id: str) -> None:
        super().__init__(coordinator, entry)
        self._category_id = category_id
        self._attr_unique_id = f"{self._plan_id}_category_{category_id}"
        self._attr_native_unit_of_measurement = coordinator.currency
        self._last_name = coordinator.data.categories[category_id].name

    @property
    def _category(self) -> Category | None:
        return self.coordinator.data.categories.get(self._category_id)

    @property
    def name(self) -> str:
        category = self._category
        if category is not None:
            self._last_name = category.name
        return self._last_name

    @property
    def available(self) -> bool:
        return super().available and self._category is not None

    @property
    def native_value(self) -> Decimal | None:
        cat = self._category
        return cat.balance if cat else None

    def _stage(self, cat: Category) -> str | None:
        mapping = self.coordinator.store.stage_mapping
        return mapping.get("categories", {}).get(cat.id) or mapping.get("groups", {}).get(
            cat.group_id
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cat = self._category
        if cat is None:
            return {}

        def num(value: Decimal | None) -> float | None:
            return None if value is None else float(value)

        return {
            "assigned": num(cat.budgeted),
            "activity": num(cat.activity),
            "target": num(cat.goal_target),
            "underfunded": num(cat.goal_under_funded),
            "target_date": cat.goal_target_date.isoformat() if cat.goal_target_date else None,
            "group": cat.group_name,
            "stage": self._stage(cat),
        }


def _unapproved(c: YnabCoordinator) -> int:
    return sum(1 for t in c.data.transactions.values() if not t.approved)


def _pending(c: YnabCoordinator) -> int:
    return len(
        [
            p
            for p in (*c.store.proposals.values(), *c.store.allocations.values())
            if p["status"] == "pending"
        ]
    )


@dataclass(frozen=True, kw_only=True)
class YnabSummarySensorDescription(SensorEntityDescription):
    """Description plus the function computing the value."""

    value_fn: Callable[[YnabCoordinator], Any]


READY_TO_ASSIGN = YnabSummarySensorDescription(
    key="ready_to_assign",
    translation_key="ready_to_assign",
    device_class=SensorDeviceClass.MONETARY,
    state_class=SensorStateClass.TOTAL,
    value_fn=lambda c: c.data.ready_to_assign,
)
UNAPPROVED = YnabSummarySensorDescription(
    key="unapproved",
    translation_key="unapproved",
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=_unapproved,
)
PROPOSALS = YnabSummarySensorDescription(
    key="proposals",
    translation_key="proposals",
    state_class=SensorStateClass.MEASUREMENT,
    value_fn=_pending,
)


class YnabSummarySensor(YnabEntity, SensorEntity):
    """Plan-level sensors: Ready to Assign, unapproved count, pending proposals."""

    entity_description: YnabSummarySensorDescription

    def __init__(
        self,
        coordinator: YnabCoordinator,
        entry: YnabConfigEntry,
        description: YnabSummarySensorDescription,
    ) -> None:
        super().__init__(coordinator, entry)
        self.entity_description = description
        self._attr_unique_id = f"{self._plan_id}_{description.key}"
        if description.device_class is SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = coordinator.currency

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)
