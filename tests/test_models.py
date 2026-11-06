"""Tests for YNAB models and money helpers."""

from datetime import date
from decimal import Decimal

import pytest

from custom_components.ynab.models import (
    Account,
    Category,
    Transaction,
    categories_from_groups,
    direction,
    to_decimal,
    to_milliunits,
)


def test_to_milliunits():
    assert to_milliunits("623.98") == 623980
    assert to_milliunits(Decimal("-623.98")) == -623980
    assert to_milliunits(1.5) == 1500
    assert to_milliunits("0.0005") == 0  # half-even
    assert to_milliunits("0.0015") == 2
    assert isinstance(to_milliunits("1"), int)


def test_to_decimal():
    assert to_decimal(-623980) == Decimal("-623.98")
    assert to_decimal(0) == Decimal("0")
    assert to_decimal(None) is None


def test_direction():
    assert direction(Decimal("-1")) == "outflow"
    assert direction(Decimal("0")) == "inflow"
    assert direction(Decimal("5")) == "inflow"


def test_account_from_api(fixture):
    raw = fixture("accounts")["data"]["accounts"]
    acc = Account.from_api(raw[0])
    assert acc.id == "acc-checking"
    assert acc.balance == Decimal("4250.75")
    assert acc.cleared_balance == Decimal("4250.75")
    assert acc.uncleared_balance == Decimal("0")
    assert acc.on_budget is True
    assert acc.closed is False
    closed = [Account.from_api(a) for a in raw if a["id"] == "acc-old"][0]
    assert closed.closed is True


def test_transaction_from_api(fixture):
    txns = {t["id"]: Transaction.from_api(t) for t in fixture("transactions")["data"]["transactions"]}
    w = txns["t-withdrawal"]
    assert w.amount == Decimal("-623.98")
    assert w.date == date(2026, 11, 3)
    assert w.account_name == "Apple Savings"
    assert w.category_id is None
    assert w.is_split is False
    assert w.approved is False
    assert txns["t-split"].is_split is True
    assert txns["t-transfer"].transfer_account_id is not None
    assert txns["t-deleted"].deleted is True


def test_categories_from_groups_order_and_group_name(fixture):
    groups = fixture("categories")["data"]["category_groups"]
    cats = categories_from_groups(groups)
    flat = [c for g in groups for c in g["categories"]]
    assert [c.id for c in cats] == [c["id"] for c in flat]
    assert [c.order for c in cats] == list(range(len(flat)))
    assert cats[0].group_name == "Bills"
    assert cats[0].goal_target == Decimal("650")
    assert cats[0].group_id == "g-bills"


def test_categories_from_groups_fills_missing_group_name():
    groups = [{"id": "g", "name": "G", "categories": [_cat(category_group_name=None)]}]
    assert categories_from_groups(groups)[0].group_name == "G"


def _cat(**kw):
    base = {
        "id": "c", "category_group_id": "g", "category_group_name": "G", "name": "C",
        "hidden": False, "deleted": False, "budgeted": 1000, "activity": -500,
        "balance": 500, "goal_type": None, "goal_target": None,
        "goal_under_funded": None, "goal_target_month": None, "goal_target_date": None,
    }
    base.update(kw)
    return base


def test_category_goal_date_fields():
    assert Category.from_api(_cat()).goal_target_date is None
    c = Category.from_api(_cat(goal_target_date="2027-03-01"))
    assert c.goal_target_date == date(2027, 3, 1)
    c = Category.from_api(_cat(goal_target_month="2027-04-01"))
    assert c.goal_target_date == date(2027, 4, 1)
    c = Category.from_api(_cat(goal_target_date="2027-03-01", goal_target_month="2027-04-01"))
    assert c.goal_target_date == date(2027, 3, 1)


def test_category_from_api_order_and_money():
    c = Category.from_api(_cat(goal_under_funded=25000), order=7)
    assert c.order == 7
    assert c.goal_under_funded == Decimal("25")
    assert c.budgeted == Decimal("1")
    assert c.balance == Decimal("0.5")
