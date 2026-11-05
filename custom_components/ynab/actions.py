"""Every write to YNAB goes through YnabActions."""

from __future__ import annotations

from decimal import Decimal
import logging
import re
from datetime import date
from typing import Any, Literal, NotRequired, Required, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .client import YnabClient, YnabError
from .const import EVENT_TRANSACTION_UPDATED
from .coordinator import YnabCoordinator
from .models import Category, Transaction, to_decimal, to_milliunits
from .store import YnabStore

_LOGGER = logging.getLogger(__name__)

MAX_MEMO_LENGTH = 500
MAX_BATCH = 100
_MONTH_RE = re.compile(r"^\d{4}-\d{2}-01$")


class TxUpdate(TypedDict):
    """One requested transaction change."""

    transaction_id: Required[str]
    payee_name: NotRequired[str]
    category: NotRequired[str]  # category id or exact name
    memo: NotRequired[str]
    approve: NotRequired[bool]
    force: NotRequired[bool]


class YnabActions:
    """Validated write operations against one YNAB plan."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: YnabClient,
        coordinator: YnabCoordinator,
        store: YnabStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.coordinator = coordinator
        self.store = store

    # -- lookups (no I/O) ---------------------------------------------------------

    def resolve_category(self, ref: str) -> Category:
        """Find an eligible category by id, else by case-insensitive exact name."""
        eligible = self.coordinator.eligible_categories()
        for cat in eligible:
            if cat.id == ref:
                return cat
        wanted = ref.strip().casefold()
        matches = [c for c in eligible if c.name.casefold() == wanted]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ServiceValidationError(f"Unknown category: {ref!r}")
        candidates = ", ".join(f"{c.name} ({c.group_name}, id {c.id})" for c in matches)
        raise ServiceValidationError(
            f"Category {ref!r} is ambiguous; use the id. Candidates: {candidates}"
        )

    def _validate_update(self, update: TxUpdate) -> tuple[Transaction, dict[str, Any]]:
        """Check one update against local state and build the YNAB PATCH dict.

        Pure: performs no I/O, so callers can validate before sending anything.
        """
        data = self.coordinator.data
        tx_id = update["transaction_id"]
        tx = data.transactions.get(tx_id) if data else None
        if tx is None:
            raise ServiceValidationError(f"Unknown transaction: {tx_id}")
        if tx.approved and not update.get("force"):
            raise ServiceValidationError(
                f"Transaction {tx_id} is already approved; pass force to change it"
            )

        patch: dict[str, Any] = {"id": tx_id}
        if update.get("category") is not None:
            if tx.is_split:
                raise ServiceValidationError(
                    f"Transaction {tx_id} is a split; its category cannot be changed here"
                )
            if tx.transfer_account_id:
                raise ServiceValidationError(
                    f"Transaction {tx_id} is a transfer; it has no category to change"
                )
            patch["category_id"] = self.resolve_category(update["category"]).id
        if update.get("payee_name") is not None:
            payee = update["payee_name"].strip()
            if not payee:
                raise ServiceValidationError("payee_name cannot be empty")
            patch["payee_name"] = payee
        if update.get("memo") is not None:
            if len(update["memo"]) > MAX_MEMO_LENGTH:
                raise ServiceValidationError(
                    f"memo is longer than {MAX_MEMO_LENGTH} characters"
                )
            patch["memo"] = update["memo"]
        if update.get("approve") is not None:
            patch["approved"] = bool(update["approve"])
        if len(patch) == 1:
            raise ServiceValidationError(f"No changes requested for transaction {tx_id}")
        return tx, patch

    # -- writes -------------------------------------------------------------------

    async def update_transactions(
        self, updates: list[TxUpdate], *, source: str
    ) -> list[str]:
        """Validate every update, then send one bulk PATCH. Returns updated ids."""
        if not updates:
            raise ServiceValidationError("No updates given")
        if len(updates) > MAX_BATCH:
            raise ServiceValidationError(f"At most {MAX_BATCH} updates per call")
        ids = [u["transaction_id"] for u in updates]
        if len(set(ids)) != len(ids):
            raise ServiceValidationError("Duplicate transaction ids in one call")

        # All-or-nothing: nothing is sent unless every update is valid.
        validated = [self._validate_update(u) for u in updates]

        try:
            await self.client.update_transactions(
                self.coordinator.plan_id, [patch for _, patch in validated]
            )
        except YnabError as err:
            raise HomeAssistantError(f"YNAB rejected the update: {err}") from err

        categories = self.coordinator.data.categories
        for _, patch in validated:
            changes = {k: v for k, v in patch.items() if k != "id"}
            if "category_id" in changes:
                cat = categories.get(changes["category_id"])
                changes["category_name"] = cat.name if cat else None
            self.store.log(
                "transaction_updated",
                transaction_id=patch["id"],
                changes=changes,
                source=source,
            )
            self.hass.bus.async_fire(
                EVENT_TRANSACTION_UPDATED,
                {
                    "entry_id": self.entry.entry_id,
                    "transaction_id": patch["id"],
                    "changes": changes,
                    "source": source,
                },
            )
        await self._after_write()
        return ids

    async def assign_money(
        self,
        category: str,
        amount: Decimal,
        *,
        month: str = "current",
        mode: Literal["set", "add"] = "set",
        source: str,
    ) -> Decimal:
        """Set or add to a category's assigned amount; returns the new amount."""
        if mode not in ("set", "add"):
            raise ServiceValidationError("mode must be 'set' or 'add'")
        if not isinstance(amount, Decimal) or not amount.is_finite():
            raise ServiceValidationError("amount must be a finite number")
        if month != "current":
            if not _MONTH_RE.match(month):
                raise ServiceValidationError("month must be 'current' or YYYY-MM-01")
            try:
                date.fromisoformat(month)
            except ValueError:
                raise ServiceValidationError("month is not a valid date") from None

        cat = self.resolve_category(category)
        data = self.coordinator.data
        if month == "current" or month == data.month:
            api_month = "current"
            existing = cat.budgeted
        else:
            api_month = month
            try:
                raw_month = await self.client.get_month(self.coordinator.plan_id, month)
            except YnabError as err:
                raise HomeAssistantError(f"Could not read month {month}: {err}") from err
            raw = next(
                (c for c in raw_month.get("categories", []) if c["id"] == cat.id), None
            )
            if raw is None:
                raise ServiceValidationError(f"{cat.name} not found in {month}")
            existing = to_decimal(raw.get("budgeted") or 0)

        new_amount = existing + amount if mode == "add" else amount
        if new_amount < 0:
            raise ServiceValidationError("Assigned amount cannot be negative")
        milli = to_milliunits(new_amount)

        try:
            await self.client.set_category_budgeted(
                self.coordinator.plan_id, api_month, cat.id, milli
            )
        except YnabError as err:
            raise HomeAssistantError(f"YNAB rejected the assignment: {err}") from err

        result = to_decimal(milli)
        self.store.log(
            "money_assigned",
            category_id=cat.id,
            category_name=cat.name,
            month=month,
            mode=mode,
            previous=float(existing),
            assigned=float(result),
            source=source,
        )
        await self._after_write()
        return result

    async def refresh(self) -> None:
        """Poll YNAB now."""
        await self.coordinator.async_request_refresh()

    async def _after_write(self) -> None:
        self.coordinator.async_update_listeners()
        await self.coordinator.async_request_refresh()
