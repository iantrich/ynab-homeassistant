"""Persistent state for the YNAB integration (HA .storage)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN

STORAGE_VERSION = 1
SAVE_DELAY_SECONDS = 10
ACTIVITY_CAP = 500


def _default_stage_mapping() -> dict[str, Any]:
    return {"groups": {}, "categories": {}, "overflow_category_id": None}


class YnabStore:
    """Everything the integration persists for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self.knowledge: dict[str, int] = {}
        self.seen_transaction_ids: set[str] = set()
        self.proposals: dict[str, dict] = {}
        self.allocations: dict[str, dict] = {}
        self.activity: list[dict] = []
        self.low_state: dict[str, bool] = {}
        self.recurring_reported: set[str] = set()
        self.stage_mapping: dict[str, Any] = _default_stage_mapping()
        self.low_share_overrides: dict[str, float] = {}
        self.initialized: bool = False

    async def async_load(self) -> None:
        """Load persisted state; missing keys fall back to defaults."""
        data = await self._store.async_load() or {}
        self.knowledge = dict(data.get("knowledge", {}))
        self.seen_transaction_ids = set(data.get("seen_transaction_ids", []))
        self.proposals = dict(data.get("proposals", {}))
        self.allocations = dict(data.get("allocations", {}))
        self.activity = list(data.get("activity", []))[-ACTIVITY_CAP:]
        self.low_state = dict(data.get("low_state", {}))
        self.recurring_reported = set(data.get("recurring_reported", []))
        self.stage_mapping = {
            **_default_stage_mapping(),
            **data.get("stage_mapping", {}),
        }
        self.low_share_overrides = dict(data.get("low_share_overrides", {}))
        self.initialized = bool(data.get("initialized", False))

    def _as_dict(self) -> dict[str, Any]:
        return {
            "knowledge": self.knowledge,
            "seen_transaction_ids": sorted(self.seen_transaction_ids),
            "proposals": self.proposals,
            "allocations": self.allocations,
            "activity": self.activity,
            "low_state": self.low_state,
            "recurring_reported": sorted(self.recurring_reported),
            "stage_mapping": self.stage_mapping,
            "low_share_overrides": self.low_share_overrides,
            "initialized": self.initialized,
        }

    def schedule_save(self) -> None:
        """Save after a short delay, coalescing bursts of changes."""
        self._store.async_delay_save(self._as_dict, SAVE_DELAY_SECONDS)

    async def async_save_now(self) -> None:
        """Save immediately."""
        await self._store.async_save(self._as_dict())

    async def async_remove(self) -> None:
        """Delete the stored file."""
        await self._store.async_remove()

    def log(self, kind: str, **fields: Any) -> None:
        """Append an activity entry (oldest dropped past the cap) and save."""
        self.activity.append(
            {"at": dt_util.utcnow().isoformat(), "kind": kind, **fields}
        )
        del self.activity[:-ACTIVITY_CAP]
        self.schedule_save()

    def prune_seen(self, keep_ids: Iterable[str]) -> None:
        """Drop seen transaction ids that are not in keep_ids."""
        self.seen_transaction_ids &= set(keep_ids)
        self.schedule_save()
