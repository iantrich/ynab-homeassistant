"""Diagnostics for the YNAB integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import YnabConfigEntry

TO_REDACT = {"api_token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: YnabConfigEntry
) -> dict[str, Any]:
    """Return diagnostics with the token redacted."""
    runtime = entry.runtime_data
    data = runtime.coordinator.data
    store = runtime.store
    return async_redact_data(
        {
            "entry": {"data": dict(entry.data), "options": dict(entry.options)},
            "counts": {
                "accounts": len(data.accounts) if data else 0,
                "categories": len(data.categories) if data else 0,
                "transactions": len(data.transactions) if data else 0,
                "proposals": len(store.proposals),
                "activity": len(store.activity),
            },
            "knowledge": dict(store.knowledge),
            "requests_last_hour": runtime.client.requests_last_hour,
            "store": store.snapshot(),
        },
        TO_REDACT,
    )
