"""Pure "low bucket" rule."""

from __future__ import annotations

from decimal import Decimal

from .models import Category


def is_low(cat: Category, share: float) -> bool:
    """Return True when a category is overspent or below ``share`` of its target.

    Callers filter out excluded groups and hidden/deleted categories.
    """
    if cat.balance < 0:
        return True
    target = cat.goal_target
    return target is not None and target > 0 and cat.balance < target * Decimal(str(share))
