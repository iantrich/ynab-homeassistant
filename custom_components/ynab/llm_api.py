"""YNAB tools exposed to LLMs as a Home Assistant LLM API.

Home Assistant's MCP Server integration serves every registered LLM API at
`/api/mcp/{api_id}` with OAuth and remote access handled by Home Assistant,
so registering the API is all this integration does to become reachable by an
MCP client. Every tool is a thin wrapper over `YnabActions` (writes pass
`source="llm"`) or over the coordinator data (reads).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import logging
from typing import TYPE_CHECKING, Any

import probatio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType

from .client import YnabError, YnabRateLimitError
from .const import DOMAIN, STAGES, TOOL_PREFIX
from .models import Category, Transaction

if TYPE_CHECKING:
    from . import YnabRuntime

_LOGGER = logging.getLogger(__name__)

SOURCE = "llm"
UNAPPROVED_LIMIT = 25
ACTIVITY_WINDOW = timedelta(hours=24)

RATE_LIMITED = (
    "YNAB is rate-limiting requests right now. Nothing was changed. Tell the "
    "user and suggest trying again later; do not retry immediately, which "
    "only extends the limit."
)

API_PROMPT = (
    "Use these tools to read the user's YNAB budget and to act on it. Amounts "
    "are in the plan's currency (see get_summary); outflows are negative.\n"
    "Before any write -- update_transactions, assign_money, approve_proposals "
    "or apply_allocation -- show the user exactly what will change and get a "
    "clear yes. Reading, proposing and rejecting need no confirmation.\n"
    "Start with get_summary for Ready to Assign, low categories, unapproved "
    "transactions and pending proposals. To spread Ready to Assign, call "
    "propose_allocation, show the moves, and only after a yes call "
    "apply_allocation with its id. Category and group names are matched "
    "case-insensitively; use ids when a name is ambiguous.\n"
    "To automate a recurring item, suggest the \"Categorize a recurring "
    "transaction\" blueprint."
)

# Annotation presets. ToolAnnotations defaults describe the least safe case,
# so anything safer has to say so explicitly.
READ_ONLY = llm.ToolAnnotations(
    read_only=True, destructive=False, idempotent=True, open_world=True
)
WRITE_ADDITIVE = llm.ToolAnnotations(
    read_only=False, destructive=False, idempotent=False, open_world=True
)
WRITE_DESTRUCTIVE = llm.ToolAnnotations(
    read_only=False, destructive=True, idempotent=True, open_world=True
)
WRITE = llm.ToolAnnotations(
    read_only=False, destructive=False, idempotent=True, open_world=True
)


def _jsonable(value: Any) -> Any:
    """Convert Decimal/date/datetime (recursively) so the result is JSON-safe."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _is_rate_limit(err: BaseException) -> bool:
    while err is not None:
        if isinstance(err, YnabRateLimitError):
            return True
        err = err.__cause__ or err.__context__  # type: ignore[assignment]
    return False


def _tx(tx: Transaction) -> dict[str, Any]:
    return {
        "id": tx.id,
        "date": tx.date.isoformat(),
        "amount": float(tx.amount),
        "account": tx.account_name,
        "payee": tx.payee_name,
        "category": tx.category_name,
        "memo": tx.memo,
        "approved": tx.approved,
        "cleared": tx.cleared,
    }


def _cat(cat: Category, low: bool) -> dict[str, Any]:
    return {
        "id": cat.id,
        "name": cat.name,
        "group": cat.group_name,
        "assigned": float(cat.budgeted),
        "activity": float(cat.activity),
        "available": float(cat.balance),
        "target": float(cat.goal_target) if cat.goal_target is not None else None,
        "low": low,
    }


def _parse_date(value: str | None, field: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ServiceValidationError(f"{field} must be YYYY-MM-DD") from None


class YnabTool(llm.Tool):
    """Base for YNAB tools."""

    integration = DOMAIN

    def __init__(self, runtime: YnabRuntime) -> None:
        """Keep the runtime (client, store, coordinator, actions) the tool uses."""
        self._runtime = runtime

    @property
    def _coordinator(self):
        return self._runtime.coordinator

    @property
    def _store(self):
        return self._runtime.store

    @property
    def _actions(self):
        return self._runtime.actions

    @property
    def _data(self):
        data = self._coordinator.data
        if data is None:
            raise HomeAssistantError("YNAB data is not loaded yet; try again shortly.")
        return data

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> llm.ToolResult:
        """Run the tool, translating failures into messages the model can act on."""
        try:
            return llm.ToolResult(data=_jsonable(await self._run(tool_input.tool_args)))
        except ServiceValidationError as err:
            raise HomeAssistantError(f"{err}. Nothing was changed.") from err
        except YnabRateLimitError as err:
            raise HomeAssistantError(RATE_LIMITED) from err
        except HomeAssistantError as err:
            if _is_rate_limit(err):
                raise HomeAssistantError(RATE_LIMITED) from err
            raise
        except YnabError as err:
            raise HomeAssistantError(f"YNAB request failed: {err}") from err

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        raise NotImplementedError


# ------------------------------------------------------------------- reads


class GetSummaryTool(YnabTool):
    """One-call overview of the budget."""

    name = f"{TOOL_PREFIX}get_summary"
    title = "Get YNAB summary"
    description = (
        "Overview of the YNAB plan: Ready to Assign, categories running low, "
        "pending proposals, the newest unapproved transactions, detected "
        "recurring transactions and what changed in the last 24 hours. Start here."
    )
    parameters = probatio.Schema({})
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        data = self._data
        coordinator = self._coordinator
        low = []
        for cat in coordinator.eligible_categories():
            if self._store.low_state.get(cat.id):
                low.append(
                    {
                        "category": cat.name,
                        "available": float(cat.balance),
                        "target": float(cat.goal_target)
                        if cat.goal_target is not None
                        else None,
                    }
                )
        unapproved = sorted(
            (t for t in data.transactions.values() if not t.approved and not t.deleted),
            key=lambda t: t.date,
            reverse=True,
        )[:UNAPPROVED_LIMIT]
        cutoff = (dt_util.utcnow() - ACTIVITY_WINDOW).isoformat()
        return {
            "currency": data.currency,
            "ready_to_assign": float(data.ready_to_assign),
            "low": low,
            "pending_proposals": self._actions.list_proposals("pending"),
            "unapproved": [_tx(t) for t in unapproved],
            "recurring": self._actions.list_recurring(),
            "activity_since": [a for a in self._store.activity if a["at"] >= cutoff],
        }


class ListAccountsTool(YnabTool):
    """Open accounts and balances."""

    name = f"{TOOL_PREFIX}list_accounts"
    title = "List YNAB accounts"
    description = "List the open YNAB accounts with their balances."
    parameters = probatio.Schema({})
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        data = self._data
        return {
            "currency": data.currency,
            "accounts": [
                {
                    "id": a.id,
                    "name": a.name,
                    "type": a.type,
                    "balance": float(a.balance),
                    "cleared_balance": float(a.cleared_balance),
                    "on_budget": a.on_budget,
                }
                for a in data.accounts.values()
                if not a.closed and not a.deleted
            ],
        }


class ListCategoriesTool(YnabTool):
    """Categories with this month's numbers."""

    name = f"{TOOL_PREFIX}list_categories"
    title = "List YNAB categories"
    description = (
        "List budget categories with assigned, activity, available and goal "
        "target for the current month. Excludes hidden categories and Credit "
        "Card Payments."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "group", description="Only categories in this group (name or id)."
            ): str,
            probatio.Optional(
                "low_only", description="If true, only categories that are running low."
            ): bool,
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        group = (args.get("group") or "").strip().casefold()
        rows = []
        for cat in self._coordinator.eligible_categories():
            if group and group not in (cat.group_name.casefold(), cat.group_id.casefold()):
                continue
            low = bool(self._store.low_state.get(cat.id))
            if args.get("low_only") and not low:
                continue
            rows.append(_cat(cat, low))
        return {"currency": self._data.currency, "categories": rows}


class ListTransactionsTool(YnabTool):
    """Recent transactions."""

    name = f"{TOOL_PREFIX}list_transactions"
    title = "List YNAB transactions"
    description = (
        "List recent transactions (about the last 120 days), newest first. "
        "Amounts are negative for outflows."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional("account", description="Account name or id."): str,
            probatio.Optional("since", description="Earliest date, YYYY-MM-DD."): str,
            probatio.Optional("until", description="Latest date, YYYY-MM-DD."): str,
            probatio.Optional(
                "approved", description="true for approved only, false for unapproved only."
            ): bool,
            probatio.Optional(
                "limit", default=50, description="Maximum results, 1 to 200 (default 50)."
            ): probatio.All(int, probatio.Range(min=1, max=200)),
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        data = self._data
        since = _parse_date(args.get("since"), "since")
        until = _parse_date(args.get("until"), "until")
        account_ids: set[str] | None = None
        if args.get("account"):
            wanted = args["account"].strip().casefold()
            account_ids = {
                a.id
                for a in data.accounts.values()
                if a.id == args["account"] or a.name.casefold() == wanted
            }
            if not account_ids:
                raise ServiceValidationError(f"Unknown account: {args['account']!r}")
        approved = args.get("approved")
        rows = [
            t
            for t in data.transactions.values()
            if not t.deleted
            and (account_ids is None or t.account_id in account_ids)
            and (since is None or t.date >= since)
            and (until is None or t.date <= until)
            and (approved is None or t.approved == approved)
        ]
        rows.sort(key=lambda t: t.date, reverse=True)
        limit = args.get("limit", 50)
        return {
            "currency": data.currency,
            "total_matching": len(rows),
            "transactions": [_tx(t) for t in rows[:limit]],
        }


class ListProposalsTool(YnabTool):
    """Stored proposals."""

    name = f"{TOOL_PREFIX}list_proposals"
    title = "List YNAB proposals"
    description = (
        "List proposals (transaction changes and allocations) waiting for a "
        "decision. Each has an id for approve_proposals / reject_proposals."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "status",
                default="pending",
                description="pending (default), approved, rejected, stale, applied or all.",
            ): probatio.In(
                ["pending", "approved", "rejected", "stale", "applied", "partial", "all"]
            ),
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        return {"proposals": self._actions.list_proposals(args.get("status", "pending"))}


class ListRecurringTool(YnabTool):
    """Detected recurring transactions."""

    name = f"{TOOL_PREFIX}list_recurring_detections"
    title = "List YNAB recurring transactions"
    description = (
        "List transactions that appear to recur monthly (same account, payee "
        "and amount). To automate one, suggest the 'Categorize a recurring "
        "transaction' blueprint."
    )
    parameters = probatio.Schema({})
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        return {"recurring": self._actions.list_recurring()}


class GetActivityLogTool(YnabTool):
    """The integration's activity log."""

    name = f"{TOOL_PREFIX}get_activity_log"
    title = "Get YNAB activity log"
    description = (
        "Recent changes made through this integration (transaction updates, "
        "money assigned, proposals), newest first, with who made them."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "limit", default=25, description="Maximum entries, 1 to 100 (default 25)."
            ): probatio.All(int, probatio.Range(min=1, max=100)),
        }
    )
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        limit = args.get("limit", 25)
        return {"activity": list(reversed(self._store.activity[-limit:]))}


# ------------------------------------------------------------------ writes

_UPDATE_ITEM = {
    probatio.Required("transaction_id", description="Id of the transaction to change."): str,
    probatio.Optional("payee_name", description="New payee name."): str,
    probatio.Optional(
        "category", description="Category name or id (not valid on splits or transfers)."
    ): str,
    probatio.Optional("memo", description="New memo (at most 500 characters)."): str,
    probatio.Optional(
        "approve", description="true to approve the transaction, false to unapprove."
    ): bool,
    probatio.Optional(
        "force",
        description="true to change an already approved transaction. Only with the user's say-so.",
    ): bool,
}


class UpdateTransactionsTool(YnabTool):
    """Change transactions."""

    name = f"{TOOL_PREFIX}update_transactions"
    title = "Update YNAB transactions"
    description = (
        "Change the category, payee, memo or approval of up to 100 "
        "transactions in one all-or-nothing call. Show the user the changes "
        "and get a clear yes first. Approved transactions are refused unless force is true."
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "updates", description="The changes to make, one entry per transaction."
            ): [_UPDATE_ITEM],
        }
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        ids = await self._actions.update_transactions(args["updates"], source=SOURCE)
        return {"updated": ids}


class AssignMoneyTool(YnabTool):
    """Assign money to a category."""

    name = f"{TOOL_PREFIX}assign_money"
    title = "Assign YNAB money"
    description = (
        "Set, or add to, the amount assigned to a category for a month. Show "
        "the user the change and get a clear yes first."
    )
    parameters = probatio.Schema(
        {
            probatio.Required("category", description="Category name or id."): str,
            probatio.Required(
                "amount", description="Amount in the plan's currency, e.g. 150 or 12.50."
            ): probatio.Coerce(str),
            probatio.Optional(
                "month",
                default="current",
                description="'current' (default) or a month as YYYY-MM-01.",
            ): str,
            probatio.Optional(
                "mode",
                default="set",
                description="'set' replaces the assigned amount; 'add' adds to it.",
            ): probatio.In(["set", "add"]),
        }
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        try:
            amount = Decimal(args["amount"].strip())
        except InvalidOperation:
            raise ServiceValidationError("amount must be a number") from None
        assigned = await self._actions.assign_money(
            args["category"],
            amount,
            month=args.get("month", "current"),
            mode=args.get("mode", "set"),
            source=SOURCE,
        )
        return {"category": args["category"], "assigned": assigned}


_IDS = probatio.Required("ids", description="Proposal ids, from list_proposals."), [str]


class ApproveProposalsTool(YnabTool):
    """Apply proposals."""

    name = f"{TOOL_PREFIX}approve_proposals"
    title = "Approve YNAB proposals"
    description = (
        "Approve pending proposals, applying them to YNAB (transaction changes "
        "and allocations). Show the user what each does and get a clear yes first."
    )
    parameters = probatio.Schema(
        {probatio.Required("ids", description="Proposal ids, from list_proposals."): [str]}
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        return await self._actions.approve_proposals(args["ids"], source=SOURCE)


class RejectProposalsTool(YnabTool):
    """Dismiss proposals."""

    name = f"{TOOL_PREFIX}reject_proposals"
    title = "Reject YNAB proposals"
    description = "Dismiss pending proposals without changing YNAB."
    parameters = probatio.Schema(
        {probatio.Required("ids", description="Proposal ids, from list_proposals."): [str]}
    )
    annotations = WRITE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        return await self._actions.reject_proposals(args["ids"], source=SOURCE)


class ProposeAllocationTool(YnabTool):
    """Plan where Ready to Assign should go."""

    name = f"{TOOL_PREFIX}propose_allocation"
    title = "Propose YNAB allocation"
    description = (
        "Work out how to spread Ready to Assign across categories using the "
        "stage mapping. Nothing is sent to YNAB; show the moves to the user, "
        "then use apply_allocation with the returned id."
    )
    parameters = probatio.Schema({})
    annotations = WRITE_ADDITIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        return {"allocation": await self._actions.propose_allocation(source=SOURCE)}


class ApplyAllocationTool(YnabTool):
    """Apply an allocation."""

    name = f"{TOOL_PREFIX}apply_allocation"
    title = "Apply YNAB allocation"
    description = (
        "Apply a pending allocation proposal, adding the moves to what is "
        "assigned. Show the user the moves and get a clear yes first."
    )
    parameters = probatio.Schema(
        {
            probatio.Required(
                "proposal_id", description="Allocation id from propose_allocation."
            ): str,
        }
    )
    annotations = WRITE_DESTRUCTIVE

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        return {
            "allocation": await self._actions.apply_allocation(
                args["proposal_id"], source=SOURCE
            )
        }


# ------------------------------------------------------------ stage mapping


class GetStageMappingTool(YnabTool):
    """Read the allocation stage mapping."""

    name = f"{TOOL_PREFIX}get_stage_mapping"
    title = "Get YNAB stage mapping"
    description = (
        "Show which allocation stage (bills, everyday, fun, savings) each "
        "category group and category is mapped to, and the overflow category."
    )
    parameters = probatio.Schema({})
    annotations = READ_ONLY

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        return self._describe()

    def _describe(self) -> dict[str, Any]:
        mapping = self._store.stage_mapping
        cats = self._data.categories
        group_names = {c.group_id: c.group_name for c in cats.values()}
        overflow_id = mapping.get("overflow_category_id")
        overflow = cats.get(overflow_id) if overflow_id else None
        return {
            "stages": list(STAGES),
            "groups": {
                group_names.get(gid, gid): stage for gid, stage in mapping["groups"].items()
            },
            "categories": {
                (cats[cid].name if cid in cats else cid): stage
                for cid, stage in mapping["categories"].items()
            },
            "overflow_category": overflow.name if overflow else None,
        }


class SetStageMappingTool(GetStageMappingTool):
    """Change the allocation stage mapping."""

    name = f"{TOOL_PREFIX}set_stage_mapping"
    title = "Set YNAB stage mapping"
    description = (
        "Map category groups and categories to allocation stages (bills, "
        "everyday, fun, savings), or 'none' to remove a mapping, and set the "
        "overflow category that receives leftover money. Only the entries "
        "given are changed. Names are matched case-insensitively."
    )
    parameters = probatio.Schema(
        {
            probatio.Optional(
                "groups",
                description="Category group name or id -> stage (bills, everyday, fun, savings) or 'none'.",
            ): {str: str},
            probatio.Optional(
                "categories",
                description="Category name or id -> stage (bills, everyday, fun, savings) or 'none'.",
            ): {str: str},
            probatio.Optional(
                "overflow_category",
                description="Category name or id that receives leftovers; null to clear.",
            ): probatio.Maybe(str),
        }
    )
    annotations = WRITE

    def _stage(self, ref: str, stage: str) -> str | None:
        key = stage.strip().casefold()
        if key == "none":
            return None
        if key not in STAGES:
            raise ServiceValidationError(
                f"Invalid stage {stage!r} for {ref!r}; use one of {', '.join(STAGES)} or 'none'"
            )
        return key

    def _group_id(self, ref: str) -> str:
        groups: dict[str, str] = {}
        for cat in self._coordinator.eligible_categories():
            groups[cat.group_id] = cat.group_name
        if ref in groups:
            return ref
        matches = [gid for gid, name in groups.items() if name.casefold() == ref.strip().casefold()]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ServiceValidationError(
                f"Unknown group: {ref!r}. Groups: {', '.join(sorted(groups.values()))}"
            )
        raise ServiceValidationError(
            f"Group {ref!r} is ambiguous; use the id. Candidates: "
            + ", ".join(f"{groups[g]} ({g})" for g in matches)
        )

    async def _run(self, args: dict[str, Any]) -> JsonObjectType:
        # Resolve and validate everything before touching the store.
        groups = {
            self._group_id(ref): self._stage(ref, stage)
            for ref, stage in (args.get("groups") or {}).items()
        }
        categories = {
            self._actions.resolve_category(ref).id: self._stage(ref, stage)
            for ref, stage in (args.get("categories") or {}).items()
        }
        overflow_given = "overflow_category" in args
        overflow_id = None
        if overflow_given and args["overflow_category"] is not None:
            overflow_id = self._actions.resolve_category(args["overflow_category"]).id

        mapping = self._store.stage_mapping
        for target, changes in (("groups", groups), ("categories", categories)):
            for key, stage in changes.items():
                if stage is None:
                    mapping[target].pop(key, None)
                else:
                    mapping[target][key] = stage
        if overflow_given:
            mapping["overflow_category_id"] = overflow_id
        self._store.schedule_save()
        await self._coordinator.async_request_refresh()
        return {"stage_mapping": self._describe()}


# --------------------------------------------------------------------- API


@dataclass(slots=True, kw_only=True)
class YnabLLMAPI(llm.API):
    """The YNAB API, as offered to LLM clients.

    `llm.API` is a slotted dataclass, so `runtime` must be a declared field.
    """

    runtime: YnabRuntime

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Build the tool set for one request."""
        today = dt_util.now()
        prompt = f"Today is {today:%A, %Y-%m-%d}.\n{API_PROMPT}"
        return llm.APIInstance(
            api=self,
            api_prompt=prompt,
            llm_context=llm_context,
            tools=_build_tools(self.runtime),
        )


def _build_tools(runtime: YnabRuntime) -> list[llm.Tool]:
    """Return every YNAB tool bound to the runtime."""
    return [
        tool(runtime)
        for tool in (
            GetSummaryTool,
            ListAccountsTool,
            ListCategoriesTool,
            ListTransactionsTool,
            UpdateTransactionsTool,
            AssignMoneyTool,
            ListProposalsTool,
            ApproveProposalsTool,
            RejectProposalsTool,
            ListRecurringTool,
            ProposeAllocationTool,
            ApplyAllocationTool,
            GetStageMappingTool,
            SetStageMappingTool,
            GetActivityLogTool,
        )
    ]


def async_register(hass: HomeAssistant, entry: ConfigEntry) -> Callable[[], None]:
    """Register the YNAB LLM API for an entry; returns the unregister callback."""
    plan_id = entry.data["plan_id"]
    api_id = f"ynab_{plan_id[:8]}"
    return llm.async_register_api(
        hass,
        YnabLLMAPI(
            hass=hass,
            id=api_id,
            name=f"YNAB ({entry.data.get('plan_name', plan_id)})",
            runtime=entry.runtime_data,
        ),
    )
