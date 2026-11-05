"""Service registration for the YNAB integration."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .actions import MAX_BATCH, YnabActions
from .const import DOMAIN

SERVICE_UPDATE_TRANSACTION = "update_transaction"
SERVICE_UPDATE_TRANSACTIONS = "update_transactions"
SERVICE_ASSIGN_MONEY = "assign_money"
SERVICE_REFRESH = "refresh"
SERVICE_PROPOSE_TRANSACTION_UPDATE = "propose_transaction_update"
SERVICE_APPROVE_PROPOSALS = "approve_proposals"
SERVICE_REJECT_PROPOSALS = "reject_proposals"
SERVICE_LIST_PROPOSALS = "list_proposals"
SERVICE_PROPOSE_ALLOCATION = "propose_allocation"
SERVICE_APPLY_ALLOCATION = "apply_allocation"


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise vol.Invalid("not a number") from None
    if not result.is_finite():
        raise vol.Invalid("not a finite number")
    return result


_UPDATE_FIELDS = {
    vol.Required("transaction_id"): cv.string,
    vol.Optional("payee_name"): cv.string,
    vol.Optional("category"): cv.string,
    vol.Optional("memo"): cv.string,
    vol.Optional("approve"): cv.boolean,
    vol.Optional("force"): cv.boolean,
}
_ENTRY = {vol.Required("config_entry_id"): cv.string}

UPDATE_TRANSACTION_SCHEMA = vol.Schema({**_ENTRY, **_UPDATE_FIELDS})
UPDATE_TRANSACTIONS_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Required("updates"): vol.All(
            cv.ensure_list, [vol.Schema(_UPDATE_FIELDS)], vol.Length(min=1, max=MAX_BATCH)
        ),
    }
)
ASSIGN_MONEY_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Required("category"): cv.string,
        vol.Required("amount"): _decimal,
        vol.Optional("month", default="current"): cv.string,
        vol.Optional("mode", default="set"): vol.In(["set", "add"]),
    }
)
REFRESH_SCHEMA = vol.Schema(_ENTRY)
_PROPOSE_FIELDS = {k: v for k, v in _UPDATE_FIELDS.items() if k != "force"}
PROPOSE_SCHEMA = vol.Schema({**_ENTRY, **_PROPOSE_FIELDS})
PROPOSAL_IDS_SCHEMA = vol.Schema(
    {**_ENTRY, vol.Required("proposal_ids"): vol.All(cv.ensure_list, [cv.string])}
)
LIST_PROPOSALS_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Optional("status", default="pending"): vol.In(
            ["pending", "approved", "rejected", "stale", "applied", "partial", "superseded", "all"]
        ),
    }
)
PROPOSE_ALLOCATION_SCHEMA = vol.Schema(_ENTRY)
APPLY_ALLOCATION_SCHEMA = vol.Schema({**_ENTRY, vol.Required("proposal_id"): cv.string})


def source_from_call(call: ServiceCall) -> str:
    """Describe who triggered a service call, for the activity log."""
    ctx = call.context
    if ctx.parent_id:
        return f"automation:{ctx.parent_id}"
    if ctx.user_id:
        return f"user:{ctx.user_id}"
    return "service"


def _actions(hass: HomeAssistant, call: ServiceCall) -> YnabActions:
    entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError("config_entry_id is not a YNAB config entry")
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError("The YNAB config entry is not loaded")
    return entry.runtime_data.actions


def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration's services (once, from async_setup)."""

    async def update_transaction(call: ServiceCall) -> dict[str, Any]:
        fields = {k: v for k, v in call.data.items() if k != "config_entry_id"}
        ids = await _actions(hass, call).update_transactions(
            [fields], source=source_from_call(call)  # type: ignore[list-item]
        )
        return {"updated": ids}

    async def update_transactions(call: ServiceCall) -> dict[str, Any]:
        ids = await _actions(hass, call).update_transactions(
            call.data["updates"], source=source_from_call(call)
        )
        return {"updated": ids}

    async def assign_money(call: ServiceCall) -> dict[str, Any]:
        assigned = await _actions(hass, call).assign_money(
            call.data["category"],
            call.data["amount"],
            month=call.data["month"],
            mode=call.data["mode"],
            source=source_from_call(call),
        )
        return {"assigned": float(assigned)}

    async def refresh(call: ServiceCall) -> None:
        await _actions(hass, call).refresh()

    async def propose_transaction_update(call: ServiceCall) -> dict[str, Any]:
        fields = {k: v for k, v in call.data.items() if k != "config_entry_id"}
        proposal_id = await _actions(hass, call).propose_transaction_update(
            fields, source=source_from_call(call)  # type: ignore[arg-type]
        )
        return {"proposal_id": proposal_id}

    async def approve_proposals(call: ServiceCall) -> dict[str, Any]:
        return await _actions(hass, call).approve_proposals(
            call.data["proposal_ids"], source=source_from_call(call)
        )

    async def reject_proposals(call: ServiceCall) -> dict[str, Any]:
        return await _actions(hass, call).reject_proposals(
            call.data["proposal_ids"], source=source_from_call(call)
        )

    async def list_proposals(call: ServiceCall) -> dict[str, Any]:
        return {"proposals": _actions(hass, call).list_proposals(call.data["status"])}

    async def propose_allocation(call: ServiceCall) -> dict[str, Any]:
        return await _actions(hass, call).propose_allocation(source=source_from_call(call))

    async def apply_allocation(call: ServiceCall) -> dict[str, Any]:
        return await _actions(hass, call).apply_allocation(
            call.data["proposal_id"], source=source_from_call(call)
        )

    for name, handler, schema, response in (
        (SERVICE_UPDATE_TRANSACTION, update_transaction, UPDATE_TRANSACTION_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_UPDATE_TRANSACTIONS, update_transactions, UPDATE_TRANSACTIONS_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_ASSIGN_MONEY, assign_money, ASSIGN_MONEY_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_REFRESH, refresh, REFRESH_SCHEMA, SupportsResponse.NONE),
        (SERVICE_PROPOSE_TRANSACTION_UPDATE, propose_transaction_update, PROPOSE_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_APPROVE_PROPOSALS, approve_proposals, PROPOSAL_IDS_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_REJECT_PROPOSALS, reject_proposals, PROPOSAL_IDS_SCHEMA, SupportsResponse.OPTIONAL),
        (SERVICE_LIST_PROPOSALS, list_proposals, LIST_PROPOSALS_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_PROPOSE_ALLOCATION, propose_allocation, PROPOSE_ALLOCATION_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_APPLY_ALLOCATION, apply_allocation, APPLY_ALLOCATION_SCHEMA, SupportsResponse.ONLY),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema=schema, supports_response=response
        )
