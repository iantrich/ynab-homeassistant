"""Tests for the waterfall allocation."""

from datetime import date
from decimal import Decimal

from custom_components.ynab.allocation import compute_allocation
from custom_components.ynab.models import Category

THIS = "2026-11-01"
NEXT = "2026-12-01"
D = Decimal
MAPPING = {
    "groups": {"g-bills": "bills", "g-everyday": "everyday", "g-fun": "fun", "g-savings": "savings"},
    "categories": {},
}


def cat(cid, group="g-bills", *, under=None, balance=0, order=0, due=None, goal="NEED"):
    return Category(
        id=cid, group_id=group, group_name=group, name=cid.title(), hidden=False,
        deleted=False, budgeted=D(0), activity=D(0), balance=D(balance), goal_type=goal,
        goal_target=None, goal_under_funded=None if under is None else D(under),
        goal_target_date=due, order=order,
    )


def run(available, this, nxt=(), mapping=MAPPING, overflow="c-over"):
    return compute_allocation(D(available), list(this), list(nxt), THIS, NEXT, mapping, overflow)


def amounts(result):
    return [(m.month, m.category_id, m.amount, m.reason) for m in result.moves]


def test_overspending_first():
    result = run(
        100,
        [cat("c-rent", under=80, order=0), cat("c-food", "g-everyday", balance=-30, order=1),
         cat("c-over", "g-fun", order=2)],
    )
    assert amounts(result) == [
        (THIS, "c-food", D("30.00"), "cover overspending"),
        (THIS, "c-rent", D("70.00"), "target"),
    ]
    assert result.total == D("100.00")
    assert result.unfunded == [("C-Rent", D("10.00"))]
    assert result.leftover_to_overflow == 0


def test_stage_order_bills_before_everyday_before_fun_before_savings():
    cats = [
        cat("c-sav", "g-savings", under=10, order=0),
        cat("c-fun", "g-fun", under=10, order=1),
        cat("c-eve", "g-everyday", under=10, order=2),
        cat("c-bill", "g-bills", under=10, order=3),
    ]
    result = run(40, cats)
    assert [m.category_id for m in result.moves] == ["c-bill", "c-eve", "c-fun", "c-sav"]
    assert [m.stage for m in result.moves] == ["bills", "everyday", "fun", "savings"]


def test_soonest_due_first_within_stage_and_partial_last():
    cats = [
        cat("c-late", under=100, order=0, due=date(2026, 11, 25)),
        cat("c-soon", under=100, order=1, due=date(2026, 11, 5)),
        cat("c-none", under=100, order=2),
    ]
    result = run(150, cats)
    assert amounts(result) == [
        (THIS, "c-soon", D("100.00"), "target"),
        (THIS, "c-late", D("50.00"), "target"),
    ]
    assert result.unfunded == [("C-Late", D("50.00")), ("C-None", D("100.00"))]


def test_late_month_paycheck_funds_next_month_before_overflow():
    this = [cat("c-rent", balance=0), cat("c-over", "g-fun", order=1)]
    nxt = [cat("c-rent", under=700), cat("c-power", under=500, order=1), cat("c-over", "g-fun", order=2)]
    result = run(1500, this, nxt)
    assert amounts(result) == [
        (NEXT, "c-rent", D("700.00"), "target"),
        (NEXT, "c-power", D("500.00"), "target"),
        (THIS, "c-over", D("300.00"), "overflow"),
    ]
    assert result.leftover_to_overflow == D("300.00")
    assert result.total == D("1500.00")


def test_balance_goal_not_double_counted_next_month():
    this = [cat("c-save", "g-savings", under=200, goal="TB"), cat("c-over", "g-fun", order=1)]
    nxt = [
        cat("c-save", "g-savings", under=500, goal="TB"),
        cat("c-need", "g-savings", under=500, goal="NEED", order=1),
    ]
    result = run(1000, this, nxt)
    # TB: next month's 500 already includes the 200 given this month -> 300 more.
    # NEED goals are monthly, so they are not reduced.
    assert amounts(result) == [
        (THIS, "c-save", D("200.00"), "target"),
        (NEXT, "c-save", D("300.00"), "target"),
        (NEXT, "c-need", D("500.00"), "target"),
    ]
    assert result.total == D("1000.00")


def test_unstaged_reported_and_unfunded():
    mapping = {"groups": {"g-bills": "bills"}, "categories": {}}
    cats = [
        cat("c-rent", under=50),
        cat("c-mystery", "g-other", under=40, order=1),
        cat("c-over", "g-fun", order=2),
    ]
    result = run(30, cats, mapping=mapping)
    assert result.unstaged == ["C-Mystery"]
    assert amounts(result) == [(THIS, "c-rent", D("30.00"), "target")]
    assert result.unfunded == [("C-Rent", D("20.00"))]


def test_category_stage_overrides_group_stage():
    mapping = {"groups": {"g-bills": "bills"}, "categories": {"c-x": "savings"}}
    result = run(100, [cat("c-x", under=10)], mapping=mapping)
    assert result.moves[0].stage == "savings"


def test_no_overflow_configured_leaves_money():
    result = run(500, [cat("c-rent", under=100)], overflow=None)
    assert amounts(result) == [(THIS, "c-rent", D("100.00"), "target")]
    assert result.leftover_to_overflow == 0
    assert result.total == D("100.00")


def test_zero_available_no_moves():
    for available in (0, -5):
        result = run(available, [cat("c-rent", under=100, balance=-5), cat("c-over", "g-fun")])
        assert result.moves == []
        assert result.total == 0


def test_unknown_stage_value_is_unstaged():
    mapping = {"groups": {"g-bills": "bogus"}, "categories": {}}
    result = run(100, [cat("c-rent", under=10), cat("c-over", "g-fun", order=1)], mapping=mapping)
    assert result.unstaged == ["C-Rent"]
    assert [m.category_id for m in result.moves] == ["c-over"]


def test_overflow_not_among_eligible_is_reported_and_money_stays():
    result = run(100, [cat("c-rent", under=10)], overflow="c-gone")
    assert result.overflow_unavailable is True
    assert result.total == D("10.00")
    assert run(100, [cat("c-rent", under=10)], overflow=None).overflow_unavailable is False


def test_balance_goal_dedupe_ignores_overspending_cover():
    this = [cat("c-save", "g-savings", balance=-50, under=100, goal="TB")]
    nxt = [cat("c-save", "g-savings", under=500, goal="TB")]
    result = run(1000, this, nxt, overflow=None)
    # 50 cover + 100 target this month; next month only the 100 target is subtracted.
    assert amounts(result)[-1] == (NEXT, "c-save", D("400.00"), "target")


def test_amounts_rounded_to_cents_and_never_exceed_available():
    result = run("10.005", [cat("c-rent", under="10.005")], overflow=None)
    assert result.moves[0].amount == D("10.00")
    assert result.total <= D("10.005")
