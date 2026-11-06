"""YNAB data models and money helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

MILLIUNITS = Decimal(1000)


def to_decimal(milliunits: int | None) -> Decimal | None:
    """Convert YNAB milliunits to a Decimal amount."""
    if milliunits is None:
        return None
    return Decimal(milliunits) / MILLIUNITS


def to_milliunits(amount: Decimal | float | str) -> int:
    """Convert an amount to integer milliunits (round half-even)."""
    return int((Decimal(str(amount)) * MILLIUNITS).quantize(Decimal(1), ROUND_HALF_EVEN))


def direction(amount: Decimal) -> Literal["inflow", "outflow"]:
    """Return whether an amount is an inflow or outflow."""
    return "outflow" if amount < 0 else "inflow"


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _money(milliunits: int | None) -> Decimal:
    return to_decimal(milliunits or 0)  # type: ignore[return-value]


@dataclass(frozen=True)
class Account:
    """A YNAB account."""

    id: str
    name: str
    type: str
    balance: Decimal
    cleared_balance: Decimal
    uncleared_balance: Decimal
    closed: bool
    deleted: bool
    on_budget: bool

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Account:
        return cls(
            id=d["id"],
            name=d["name"],
            type=d["type"],
            balance=_money(d.get("balance")),
            cleared_balance=_money(d.get("cleared_balance")),
            uncleared_balance=_money(d.get("uncleared_balance")),
            closed=bool(d.get("closed", False)),
            deleted=bool(d.get("deleted", False)),
            on_budget=bool(d.get("on_budget", True)),
        )


@dataclass(frozen=True)
class Category:
    """A YNAB category for one month."""

    id: str
    group_id: str
    group_name: str
    name: str
    hidden: bool
    deleted: bool
    budgeted: Decimal
    activity: Decimal
    balance: Decimal
    goal_type: str | None
    goal_target: Decimal | None
    goal_under_funded: Decimal | None
    goal_target_date: date | None
    order: int

    @classmethod
    def from_api(
        cls, d: dict[str, Any], order: int = 0, group_name: str | None = None
    ) -> Category:
        """Parse one category dict.

        goal_target_month is deprecated in favour of goal_target_date; accept both.
        """
        return cls(
            id=d["id"],
            group_id=d["category_group_id"],
            group_name=d.get("category_group_name") or group_name or "",
            name=d["name"],
            hidden=bool(d.get("hidden", False)),
            deleted=bool(d.get("deleted", False)),
            budgeted=_money(d.get("budgeted")),
            activity=_money(d.get("activity")),
            balance=_money(d.get("balance")),
            goal_type=d.get("goal_type"),
            goal_target=to_decimal(d.get("goal_target")),
            goal_under_funded=to_decimal(d.get("goal_under_funded")),
            goal_target_date=_date(d.get("goal_target_date") or d.get("goal_target_month")),
            order=order,
        )


def categories_from_groups(groups: list[dict[str, Any]]) -> list[Category]:
    """Flatten a category_groups response, assigning group_name and API order."""
    cats: list[Category] = []
    for group in groups:
        for raw in group.get("categories", []):
            cats.append(Category.from_api(raw, order=len(cats), group_name=group.get("name")))
    return cats


@dataclass(frozen=True)
class Transaction:
    """A YNAB transaction."""

    id: str
    date: date
    amount: Decimal
    account_id: str
    account_name: str | None
    payee_name: str | None
    import_payee_name: str | None
    import_payee_name_original: str | None
    category_id: str | None
    category_name: str | None
    memo: str | None
    approved: bool
    cleared: str
    deleted: bool
    transfer_account_id: str | None
    is_split: bool
    import_id: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> Transaction:
        return cls(
            id=d["id"],
            date=date.fromisoformat(d["date"]),
            amount=_money(d.get("amount")),
            account_id=d["account_id"],
            account_name=d.get("account_name"),
            payee_name=d.get("payee_name"),
            import_payee_name=d.get("import_payee_name"),
            import_payee_name_original=d.get("import_payee_name_original"),
            category_id=d.get("category_id"),
            category_name=d.get("category_name"),
            memo=d.get("memo"),
            approved=bool(d.get("approved", False)),
            cleared=d.get("cleared", "uncleared"),
            deleted=bool(d.get("deleted", False)),
            transfer_account_id=d.get("transfer_account_id"),
            is_split=bool(d.get("subtransactions")),
            import_id=d.get("import_id"),
        )
