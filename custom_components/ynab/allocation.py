"""Waterfall allocation of Ready to Assign money (pure; no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Any

from .const import STAGES
from .models import Category

CENT = Decimal("0.01")
BALANCE_GOAL_TYPES = {"TB", "TBD"}
OVERSPENDING_STAGE = "overspending"
OVERFLOW_STAGE = "overflow"


@dataclass
class Move:
    """Assign ``amount`` more to a category in a month."""

    month: str
    category_id: str
    category_name: str
    amount: Decimal
    stage: str
    reason: str


@dataclass
class AllocationResult:
    """What the waterfall would do with the available money."""

    moves: list[Move] = field(default_factory=list)
    total: Decimal = Decimal(0)
    leftover_to_overflow: Decimal = Decimal(0)
    unfunded: list[tuple[str, Decimal]] = field(default_factory=list)
    unstaged: list[str] = field(default_factory=list)


def _stage(cat: Category, mapping: dict[str, Any]) -> str | None:
    return (mapping.get("categories") or {}).get(cat.id) or (
        mapping.get("groups") or {}
    ).get(cat.group_id)


def _cents(amount: Decimal) -> Decimal:
    # Round down so the moves can never add up to more than the money available.
    return amount.quantize(CENT, rounding=ROUND_DOWN)


def compute_allocation(
    available: Decimal,
    this_month: list[Category],
    next_month: list[Category],
    this_month_key: str,
    next_month_key: str,
    mapping: dict,
    overflow_category_id: str | None,
) -> AllocationResult:
    """Propose moves in priority order: overspending, then each stage this
    month, then each stage next month, then the overflow category.

    The caller passes only eligible categories.
    """
    result = AllocationResult()
    remaining = Decimal(available)
    assigned_this_month: dict[str, Decimal] = {}

    def underfunded(cat: Category) -> Decimal:
        return cat.goal_under_funded if cat.goal_under_funded and cat.goal_under_funded > 0 else Decimal(0)

    unstaged: list[str] = []
    for cat in (*this_month, *next_month):
        if underfunded(cat) > 0 and _stage(cat, mapping) is None and cat.name not in unstaged:
            unstaged.append(cat.name)
    result.unstaged = unstaged

    def add(month: str, cat: Category, wanted: Decimal, stage: str, reason: str) -> Decimal:
        """Move min(wanted, remaining); returns the amount actually moved."""
        nonlocal remaining
        amount = _cents(min(wanted, remaining))
        if amount <= 0:
            return Decimal(0)
        result.moves.append(Move(month, cat.id, cat.name, amount, stage, reason))
        remaining -= amount
        if month == this_month_key:
            assigned_this_month[cat.id] = assigned_this_month.get(cat.id, Decimal(0)) + amount
        return amount

    if remaining > 0:
        for cat in sorted(this_month, key=lambda c: c.order):
            if cat.balance < 0:
                add(this_month_key, cat, -cat.balance, OVERSPENDING_STAGE, "cover overspending")

        for month_key, cats in ((this_month_key, this_month), (next_month_key, next_month)):
            for stage in STAGES:
                staged = sorted(
                    (c for c in cats if underfunded(c) > 0 and _stage(c, mapping) == stage),
                    key=lambda c: (c.goal_target_date or date.max, c.order),
                )
                for cat in staged:
                    wanted = underfunded(cat)
                    if month_key == next_month_key and cat.goal_type in BALANCE_GOAL_TYPES:
                        # A balance goal counts what this run already put in.
                        wanted -= assigned_this_month.get(cat.id, Decimal(0))
                    if wanted <= 0:
                        continue
                    given = add(month_key, cat, wanted, stage, "target")
                    if given < wanted:
                        result.unfunded.append((cat.name, _cents(wanted - given)))

        overflow = next(
            (c for c in this_month if c.id == overflow_category_id), None
        ) if overflow_category_id else None
        if overflow is not None and remaining > 0:
            result.leftover_to_overflow = add(
                this_month_key, overflow, remaining, OVERFLOW_STAGE, "overflow"
            )
    else:
        for month_key, cats in ((this_month_key, this_month), (next_month_key, next_month)):
            for cat in cats:
                if underfunded(cat) > 0 and _stage(cat, mapping) is not None:
                    result.unfunded.append((cat.name, _cents(underfunded(cat))))

    result.total = sum((m.amount for m in result.moves), Decimal(0))
    return result
