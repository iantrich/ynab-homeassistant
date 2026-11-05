"""Detect transactions that recur monthly (pure)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import Transaction, to_milliunits

MONTHS_LOOKED_BACK = 3
MAX_DAY_SPREAD = 3
MIN_DISTINCT_MONTHS = 2


@dataclass(frozen=True)
class Recurring:
    """A transaction that shows up around the same day every month."""

    key: str
    account_id: str
    account_name: str | None
    import_payee_name: str | None
    amount: Decimal
    typical_day: int
    category_id: str
    category_name: str | None
    payee_name: str | None
    occurrences: list[date]

    def as_event_data(self) -> dict:
        """JSON-safe form: amount as float, dates as ISO strings."""
        return {
            "key": self.key,
            "account_id": self.account_id,
            "account_name": self.account_name,
            "import_payee_name": self.import_payee_name,
            "amount": float(self.amount),
            "typical_day": self.typical_day,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "payee_name": self.payee_name,
            "occurrences": [d.isoformat() for d in self.occurrences],
        }


def _month_index(d: date) -> int:
    return d.year * 12 + d.month


def _median_day(days: list[int]) -> int:
    ordered = sorted(days)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def detect_recurring(transactions: Iterable[Transaction], today: date) -> list[Recurring]:
    """Find categorised transactions that repeat monthly.

    Considers approved, non-deleted, non-split, non-transfer transactions with a
    category in the current calendar month and the two before it.
    """
    newest = _month_index(today)
    oldest = newest - (MONTHS_LOOKED_BACK - 1)
    groups: dict[tuple[str, str, int], list[Transaction]] = defaultdict(list)
    for tx in transactions:
        payee = tx.import_payee_name or tx.payee_name
        if (
            not tx.approved
            or tx.deleted
            or tx.is_split
            or tx.transfer_account_id
            or not tx.category_id
            or not payee
            or not oldest <= _month_index(tx.date) <= newest
        ):
            continue
        groups[(tx.account_id, payee, to_milliunits(tx.amount))].append(tx)

    found: list[Recurring] = []
    for (account_id, payee, milli), txs in groups.items():
        if len({_month_index(t.date) for t in txs}) < MIN_DISTINCT_MONTHS:
            continue
        days = [t.date.day for t in txs]
        typical = _median_day(days)
        if any(abs(day - typical) > MAX_DAY_SPREAD for day in days):
            continue
        latest_date = max(t.date for t in txs)
        latest = [t for t in txs if t.date == latest_date]
        if len({t.category_id for t in latest}) != 1:
            continue
        last = latest[0]
        found.append(
            Recurring(
                key=f"{account_id}|{payee}|{milli}",
                account_id=account_id,
                account_name=last.account_name,
                import_payee_name=last.import_payee_name,
                amount=last.amount,
                typical_day=typical,
                category_id=last.category_id,
                category_name=last.category_name,
                payee_name=last.payee_name,
                occurrences=sorted(t.date for t in txs),
            )
        )
    return sorted(found, key=lambda r: r.key)
