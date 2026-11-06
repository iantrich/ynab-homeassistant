"""Tests for the low-bucket rule."""

from datetime import date
from decimal import Decimal

from custom_components.ynab.low import is_low
from custom_components.ynab.models import Category


def _cat(balance, target) -> Category:
    return Category(
        id="c", group_id="g", group_name="G", name="N", hidden=False, deleted=False,
        budgeted=Decimal(0), activity=Decimal(0), balance=Decimal(str(balance)),
        goal_type=None, goal_target=None if target is None else Decimal(str(target)),
        goal_under_funded=None, goal_target_date=None, order=0,
    )


def test_overspent_is_low():
    assert is_low(_cat(-0.01, 100), 0.2)
    assert is_low(_cat(-5, None), 0.2)


def test_below_share_of_target_is_low():
    assert is_low(_cat(19, 100), 0.2)


def test_at_share_of_target_is_not_low():
    assert not is_low(_cat(20, 100), 0.2)


def test_no_target_positive_balance_not_low():
    assert not is_low(_cat(5, None), 0.2)
    assert not is_low(_cat(5, 0), 0.2)
    assert not is_low(_cat(0, None), 0.2)
