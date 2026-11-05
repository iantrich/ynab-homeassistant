"""Every write to YNAB goes through YnabActions."""

from __future__ import annotations

from decimal import Decimal
import logging
import re
from datetime import date, timedelta
import secrets
from typing import Any, Literal, NotRequired, Required, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .allocation import compute_allocation
from .client import YnabClient, YnabError
from .const import EVENT_PROPOSAL_CREATED, EVENT_TRANSACTION_UPDATED
from .coordinator import YnabCoordinator, YnabData
from .models import Category, Transaction, to_decimal, to_milliunits
from .store import YnabStore

_LOGGER = logging.getLogger(__name__)

MAX_MEMO_LENGTH = 500
MAX_BATCH = 100
RESOLVED_RETENTION = timedelta(days=30)
CREDIT_CARD_GROUP_NAME = "Credit Card Payments"
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

    def _transaction_categories(self) -> list[Category]:
        """Categories a transaction may use.

        Unlike allocation, this includes "Inflow: Ready to Assign" (YNAB's
        normal category for income) but still excludes Credit Card Payments.
        """
        data = self.coordinator.data
        if data is None:
            return []
        return [
            c
            for c in data.categories.values()
            if not c.hidden
            and not c.deleted
            and c.group_name != CREDIT_CARD_GROUP_NAME
        ]

    def resolve_category(self, ref: str, *, for_transaction: bool = False) -> Category:
        """Find a category by id, else by case-insensitive exact name.

        Defaults to the allocation-eligible set (used by assign_money); with
        ``for_transaction`` the wider transaction-categorization set is used.
        """
        eligible = (
            self._transaction_categories()
            if for_transaction
            else self.coordinator.eligible_categories()
        )
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
        tx_id = update.get("transaction_id")
        if not tx_id or not isinstance(tx_id, str):
            raise ServiceValidationError("transaction_id is required")
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
            patch["category_id"] = self.resolve_category(update["category"], for_transaction=True).id
        if update.get("payee_name") is not None:
            if tx.transfer_account_id:
                raise ServiceValidationError(
                    f"Transaction {tx_id} is a transfer; changing its payee would "
                    "break the link to the other account"
                )
            payee = update["payee_name"].strip()
            if not payee:
                raise ServiceValidationError("payee_name cannot be empty")
            patch["payee_name"] = payee
            patch["payee_id"] = None  # so YNAB matches/creates by name
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
        # All-or-nothing: nothing is sent unless every update is valid.
        validated = [self._validate_update(u) for u in updates]
        ids = [tx.id for tx, _ in validated]
        if len(set(ids)) != len(ids):
            raise ServiceValidationError("Duplicate transaction ids in one call")

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
            api_month = data.month  # explicit, so a rollover can't mismatch
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


    # -- allocation ---------------------------------------------------------------

    async def _fetch_month_categories(
        self, month: str, current: dict[str, Category]
    ) -> tuple[dict[str, Any], list[Category]]:
        """Read one month; keeps only categories the coordinator considers eligible."""
        try:
            raw_month = await self.client.get_month(self.coordinator.plan_id, month)
        except YnabError as err:
            raise HomeAssistantError(f"Could not read month {month}: {err}") from err
        cats = []
        for raw in raw_month.get("categories", []):
            known = current.get(raw["id"])
            if known is None:
                continue
            cats.append(Category.from_api(raw, order=known.order, group_name=known.group_name))
        return raw_month, cats

    async def propose_allocation(self, *, source: str) -> dict[str, Any]:
        """Work out where Ready to Assign should go; nothing is sent to YNAB."""
        data = self.coordinator.data
        if data is None:
            raise ServiceValidationError("YNAB data is not loaded yet")
        this_key = data.month
        year, month = int(this_key[:4]), int(this_key[5:7])
        next_key = f"{year + month // 12}-{month % 12 + 1:02d}-01"

        eligible = {c.id: c for c in self.coordinator.eligible_categories()}
        raw_this, this_cats = await self._fetch_month_categories(this_key, eligible)
        _, next_cats = await self._fetch_month_categories(next_key, eligible)
        available = to_decimal(raw_this.get("to_be_budgeted") or 0)

        mapping = self.store.stage_mapping
        result = compute_allocation(
            available, this_cats, next_cats, this_key, next_key,
            mapping, mapping.get("overflow_category_id"),
        )

        allocations = self.store.allocations
        for earlier in allocations.values():
            if earlier["status"] == "pending":
                self._resolve(earlier, "superseded")
        allocation_id = "a-" + secrets.token_hex(3)
        while allocation_id in allocations:
            allocation_id = "a-" + secrets.token_hex(3)
        proposal = {
            "id": allocation_id,
            "kind": "allocation",
            "moves": [
                {
                    "month": m.month,
                    "category_id": m.category_id,
                    "category_name": m.category_name,
                    "amount": str(m.amount),
                    "stage": m.stage,
                    "reason": m.reason,
                }
                for m in result.moves
            ],
            "total": str(result.total),
            "leftover_to_overflow": str(result.leftover_to_overflow),
            "unfunded": [[name, str(amount)] for name, amount in result.unfunded],
            "unstaged": result.unstaged,
            "rta_at_creation": str(available),
            "source": source,
            "created_at": dt_util.utcnow().isoformat(),
            "status": "pending",
        }
        allocations[allocation_id] = proposal
        summary = (
            f"Assign {result.total} of {available} Ready to Assign across "
            f"{len(result.moves)} moves"
        )
        self.store.log(
            "allocation_proposed",
            proposal_id=allocation_id,
            total=str(result.total),
            moves=len(result.moves),
            source=source,
        )
        self.hass.bus.async_fire(
            EVENT_PROPOSAL_CREATED,
            {
                "entry_id": self.entry.entry_id,
                "proposal_id": allocation_id,
                "kind": "allocation",
                "summary": summary,
            },
        )
        self._proposals_changed()
        return dict(proposal)

    async def apply_allocation(self, proposal_id: str, *, source: str) -> dict[str, Any]:
        """Apply a pending allocation move by move (adding to what is assigned)."""
        proposal = self.store.allocations.get(proposal_id)
        if proposal is None:
            raise ServiceValidationError(f"Unknown allocation: {proposal_id}")
        if proposal["status"] != "pending":
            raise ServiceValidationError(
                f"Allocation {proposal_id} is {proposal['status']}, not pending"
            )
        total = Decimal(proposal["total"])
        try:
            raw_month = await self.client.get_month(self.coordinator.plan_id, "current")
        except YnabError as err:
            raise HomeAssistantError(f"Could not read the current month: {err}") from err
        available = to_decimal(raw_month.get("to_be_budgeted") or 0)
        if available < total:
            raise ServiceValidationError(
                f"ready_to_assign_dropped: {available} is available but the "
                f"allocation needs {total}; propose a new one"
            )

        # One write per (month, category): assign_money reads the cached
        # amount for the current month, so repeated adds would overwrite.
        groups: dict[tuple[str, str], list[dict]] = {}
        for move in proposal["moves"]:
            groups.setdefault((move["month"], move["category_id"]), []).append(move)

        applied: list[dict] = []
        for (month, category_id), moves in groups.items():
            amount = sum((Decimal(m["amount"]) for m in moves), Decimal(0))
            try:
                await self.assign_money(
                    category_id, amount, month=month, mode="add", source=source
                )
            except HomeAssistantError as err:
                if not applied:
                    raise
                proposal["applied_moves"] = applied
                proposal["status"] = "partial"
                proposal["resolved_at"] = dt_util.utcnow().isoformat()
                self.store.log(
                    "allocation_partial",
                    proposal_id=proposal_id,
                    applied=len(applied),
                    source=source,
                )
                self._proposals_changed()
                done = ", ".join(
                    f"{m['category_name']} {m['amount']} ({m['month']})" for m in applied
                )
                raise HomeAssistantError(
                    f"Allocation stopped at {moves[0]['category_name']} ({err}). "
                    f"Already applied: {done}"
                ) from err
            applied.extend(moves)

        proposal["applied_moves"] = applied
        self._resolve(proposal, "applied")
        self.store.log(
            "allocation_applied", proposal_id=proposal_id, total=str(total), source=source
        )
        self._proposals_changed()
        return dict(proposal)

    # -- proposals ----------------------------------------------------------------

    async def propose_transaction_update(self, update: TxUpdate, *, source: str) -> str:
        """Record a suggested change; nothing is sent to YNAB until approved."""
        # Approval never forces, so a proposal for an approved transaction is moot.
        checked = {k: v for k, v in update.items() if k != "force"}
        tx, patch = self._validate_update(checked)  # type: ignore[arg-type]

        changes: dict[str, Any] = {}
        if "category_id" in patch:
            cat = self.coordinator.data.categories.get(patch["category_id"])
            changes["category_id"] = patch["category_id"]
            changes["category_name"] = cat.name if cat else None
        if "payee_name" in patch:
            changes["payee_name"] = patch["payee_name"]
        if "memo" in patch:
            changes["memo"] = patch["memo"]
        if "approved" in patch:
            changes["approve"] = patch["approved"]

        proposals = self.store.proposals
        existing = next(
            (
                p
                for p in proposals.values()
                if p["status"] == "pending"
                and p["kind"] == "transaction"
                and p["transaction_id"] == tx.id
            ),
            None,
        )
        if existing is not None:
            proposal_id = existing["id"]
        else:
            proposal_id = "p-" + secrets.token_hex(3)
            while proposal_id in proposals:
                proposal_id = "p-" + secrets.token_hex(3)
        proposals[proposal_id] = {
            "id": proposal_id,
            "kind": "transaction",
            "transaction_id": tx.id,
            "changes": changes,
            "snapshot": {
                "date": tx.date.isoformat(),
                "amount": float(tx.amount),
                "account_name": tx.account_name,
                "import_payee_name": tx.import_payee_name,
                "category_id": tx.category_id,
            },
            "source": source,
            "created_at": dt_util.utcnow().isoformat(),
            "status": "pending",
        }
        summary = (
            f"{tx.account_name or 'Account'} {tx.date.isoformat()} {float(tx.amount):.2f}"
            f" ({tx.payee_name or tx.import_payee_name or 'no payee'}): "
            + ", ".join(
                f"{k}={changes.get('category_name') if k == 'category_id' else v}"
                for k, v in changes.items()
                if k != "category_name"
            )
        )
        self.store.log(
            "proposal_created",
            proposal_id=proposal_id,
            transaction_id=tx.id,
            source=source,
        )
        self.hass.bus.async_fire(
            EVENT_PROPOSAL_CREATED,
            {
                "entry_id": self.entry.entry_id,
                "proposal_id": proposal_id,
                "kind": "transaction",
                "summary": summary,
            },
        )
        self._proposals_changed()
        return proposal_id

    def list_proposals(self, status: str = "pending") -> list[dict]:
        """Proposals with the given status ("all" for every one), oldest first."""
        return sorted(
            (
                dict(p)
                for p in (*self.store.proposals.values(), *self.store.allocations.values())
                if status == "all" or p["status"] == status
            ),
            key=lambda p: p["created_at"],
        )

    async def approve_proposals(self, ids: list[str], *, source: str) -> dict[str, Any]:
        """Apply pending transaction proposals in one bulk update."""
        proposals = self.store.proposals
        skipped: dict[str, str] = {}
        ready: list[tuple[dict, TxUpdate]] = []
        data = self.coordinator.data
        allocation_ids: list[str] = []
        for pid in dict.fromkeys(ids):
            if pid in self.store.allocations:
                allocation_ids.append(pid)
                continue
            proposal = proposals.get(pid)
            if proposal is None:
                skipped[pid] = "unknown"
            elif proposal["status"] != "pending":
                skipped[pid] = proposal["status"]
            elif proposal["kind"] != "transaction":
                skipped[pid] = "unsupported_kind"
            elif _is_stale(proposal, data):
                self._resolve(proposal, "stale")
                skipped[pid] = "stale"
            else:
                changes = proposal["changes"]
                update: TxUpdate = {"transaction_id": proposal["transaction_id"]}
                if "category_id" in changes:
                    update["category"] = changes["category_id"]
                if "payee_name" in changes:
                    update["payee_name"] = changes["payee_name"]
                if "memo" in changes:
                    update["memo"] = changes["memo"]
                update["approve"] = changes.get("approve", True)
                try:
                    self._validate_update(update)
                except ServiceValidationError as err:
                    skipped[pid] = str(err)  # stays pending; may be fixable
                    continue
                ready.append((proposal, update))

        if ready:
            # Resolve first: the refresh inside update_transactions runs the
            # stale listener, which must not see these as still pending.
            for proposal, _ in ready:
                self._resolve(proposal, "approved")
            try:
                await self.update_transactions([u for _, u in ready], source=source)
            except Exception:
                for proposal, _ in ready:
                    proposal["status"] = "pending"
                    proposal.pop("resolved_at", None)
                self._proposals_changed()
                raise
            self.store.log(
                "proposals_approved",
                proposal_ids=[p["id"] for p, _ in ready],
                source=source,
            )
        applied = [p["id"] for p, _ in ready]
        for pid in allocation_ids:
            if self.store.allocations[pid]["status"] != "pending":
                skipped[pid] = self.store.allocations[pid]["status"]
                continue
            try:
                await self.apply_allocation(pid, source=source)
            except ServiceValidationError as err:
                skipped[pid] = str(err)  # stays pending
                continue
            applied.append(pid)
        if ready or skipped:
            self._proposals_changed()
        return {"applied": applied, "skipped": skipped}

    async def reject_proposals(self, ids: list[str], *, source: str) -> dict[str, Any]:
        """Dismiss pending proposals without changing YNAB."""
        rejected: list[str] = []
        skipped: dict[str, str] = {}
        for pid in dict.fromkeys(ids):
            proposal = self.store.proposals.get(pid) or self.store.allocations.get(pid)
            if proposal is None:
                skipped[pid] = "unknown"
            elif proposal["status"] != "pending":
                skipped[pid] = proposal["status"]
            else:
                self._resolve(proposal, "rejected")
                rejected.append(pid)
        if rejected:
            self.store.log("proposals_rejected", proposal_ids=rejected, source=source)
            self._proposals_changed()
        return {"rejected": rejected, "skipped": skipped}

    def on_synced(self, old: YnabData | None, new: YnabData) -> None:
        """Sync listener: mark outdated proposals stale and prune old ones."""
        changed = False
        for proposal in self.store.proposals.values():
            if (
                proposal["status"] == "pending"
                and proposal["kind"] == "transaction"
                and _is_stale(proposal, new)
            ):
                self._resolve(proposal, "stale")
                changed = True
        cutoff = dt_util.utcnow() - RESOLVED_RETENTION
        old_ids = []
        for table in (self.store.proposals, self.store.allocations):
            expired = [
                pid
                for pid, p in table.items()
                if p["status"] != "pending"
                and dt_util.parse_datetime(p.get("resolved_at") or p["created_at"]) < cutoff
            ]
            for pid in expired:
                del table[pid]
            old_ids += expired
        if changed or old_ids:
            self.store.schedule_save()

    @staticmethod
    def _resolve(proposal: dict, status: str) -> None:
        proposal["status"] = status
        proposal["resolved_at"] = dt_util.utcnow().isoformat()

    def _proposals_changed(self) -> None:
        self.store.schedule_save()
        self.coordinator.async_update_listeners()

    async def refresh(self) -> None:
        """Poll YNAB now."""
        await self.coordinator.async_request_refresh()

    async def _after_write(self) -> None:
        self.coordinator.async_update_listeners()
        await self.coordinator.async_request_refresh()


def _is_stale(proposal: dict, data: YnabData | None) -> bool:
    """A transaction proposal is stale once its transaction changed underneath it."""
    tx = data.transactions.get(proposal["transaction_id"]) if data else None
    return (
        tx is None
        or tx.approved
        or tx.category_id != proposal["snapshot"]["category_id"]
    )
