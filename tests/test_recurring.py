"""Tests for recurring transaction detection."""

import copy
from datetime import date, timedelta
from decimal import Decimal

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.ynab.models import Transaction
from custom_components.ynab.recurring import detect_recurring
from tests.conftest import BASE, _register

TODAY = date(2026, 11, 5)


def _tx(day, *, month=None, amount="-650", approved=True, deleted=False, split=False,
        transfer=None, category="c-housing", payee="Mortgage Co", import_payee=None,
        account="acc-1", n=[0]):
    n[0] += 1
    return Transaction(
        id=f"t{n[0]}",
        date=date(2026, month or 11, day),
        amount=Decimal(amount),
        account_id=account,
        account_name="Checking",
        payee_name=payee,
        import_payee_name=import_payee,
        import_payee_name_original=None,
        category_id=category,
        category_name="Housing" if category else None,
        memo=None,
        approved=approved,
        cleared="cleared",
        deleted=deleted,
        transfer_account_id=transfer,
        is_split=split,
    )


def _mortgage():
    return [_tx(2, month=9), _tx(1, month=10), _tx(3, month=11)]


def test_detects_monthly_mortgage():
    [rec] = detect_recurring(_mortgage(), TODAY)
    assert rec.typical_day == 2
    assert rec.key == "acc-1|Mortgage Co|-650000"
    assert rec.amount == Decimal("-650")
    assert rec.category_id == "c-housing"
    assert rec.category_name == "Housing"
    assert rec.account_name == "Checking"
    assert rec.occurrences == [date(2026, 9, 2), date(2026, 10, 1), date(2026, 11, 3)]


def test_import_payee_name_preferred_for_grouping():
    txs = [_tx(d, month=m, import_payee="MORTGAGE CO 123", payee=p)
           for d, m, p in ((2, 9, "A"), (1, 10, "B"), (3, 11, "C"))]
    [rec] = detect_recurring(txs, TODAY)
    assert rec.key == "acc-1|MORTGAGE CO 123|-650000"
    assert rec.import_payee_name == "MORTGAGE CO 123"


def test_amount_differing_by_one_cent_not_detected():
    txs = [_tx(2, month=9), _tx(1, month=10, amount="-650.01")]
    assert detect_recurring(txs, TODAY) == []


def test_days_too_far_apart_not_detected():
    assert detect_recurring([_tx(1, month=10), _tx(15, month=11)], TODAY) == []


def test_single_month_not_detected():
    assert detect_recurring([_tx(1), _tx(3)], TODAY) == []


def test_older_than_three_calendar_months_ignored():
    txs = [_tx(2, month=7), _tx(2, month=8), _tx(2, month=11)]
    assert detect_recurring(txs, TODAY) == []


def test_unapproved_ignored():
    txs = [_tx(2, month=9), _tx(3, month=11, approved=False)]
    assert detect_recurring(txs, TODAY) == []


@pytest.mark.parametrize(
    "bad", [{"deleted": True}, {"split": True}, {"transfer": "acc-2"}, {"category": None}]
)
def test_ineligible_transactions_ignored(bad):
    txs = [_tx(2, month=9), _tx(1, month=10, **bad)]
    assert detect_recurring(txs, TODAY) == []


def test_uses_latest_categorisation_only():
    txs = [_tx(2, month=9, category="c-old"), _tx(1, month=10, category="c-new"),
           _tx(3, month=11, category="c-new")]
    [rec] = detect_recurring(txs, TODAY)
    assert rec.category_id == "c-new"


def test_latest_occurrence_mixed_categories_not_detected():
    txs = [_tx(2, month=9), _tx(3, month=11, category="c-a"), _tx(3, month=11, category="c-b")]
    assert detect_recurring(txs, TODAY) == []


def test_year_boundary_window():
    txs = [Transaction(**{**_tx(5).__dict__, "id": f"y{i}", "date": d})
           for i, d in enumerate((date(2025, 11, 4), date(2025, 12, 5), date(2026, 1, 6)))]
    assert len(detect_recurring(txs, date(2026, 1, 20))) == 1


def _mortgage_json(i, day, month):
    return {
        "id": f"rec-{i}", "date": f"2026-{month:02d}-{day:02d}", "amount": -650000,
        "memo": None, "cleared": "cleared", "approved": True, "account_id": "acc-savings",
        "account_name": "Apple Savings", "payee_name": "Mortgage Co", "category_id": "c-groceries",
        "category_name": "Groceries", "transfer_account_id": None, "import_payee_name": None,
        "import_payee_name_original": None, "deleted": False, "subtransactions": [],
    }


async def _setup(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    entry = MockConfigEntry(
        domain="ynab", title="Household",
        data={"api_token": "tok", "plan_id": "p", "plan_name": "Household", "currency": "USD"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _resync(hass, aioclient_mock, fixture, k):
    txs = copy.deepcopy(fixture("transactions"))
    txs["data"]["transactions"] += [
        _mortgage_json(1, 2, 9), _mortgage_json(2, 1, 10), _mortgage_json(3, 3, 11)
    ]
    _register(aioclient_mock, fixture, txs=txs, k=k)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=16))
    await hass.async_block_till_done()


async def test_event_fires_once_across_two_syncs(hass, aioclient_mock, fixture, freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")
    entry = await _setup(hass, aioclient_mock, fixture)
    events = async_capture_events(hass, "ynab_recurring_detected")
    await _resync(hass, aioclient_mock, fixture, (1101, 1102, 1103))
    assert len(events) == 1
    data = events[0].data
    assert data["entry_id"] == entry.entry_id
    assert data["key"] == "acc-savings|Mortgage Co|-650000"
    assert data["amount"] == -650.0
    assert data["typical_day"] == 2
    assert data["occurrences"] == ["2026-09-02", "2026-10-01", "2026-11-03"]
    assert data["category_id"] == "c-groceries"
    assert data["key"] in entry.runtime_data.store.recurring_reported

    freezer.move_to("2026-11-05T12:20:00+00:00")
    await _resync(hass, aioclient_mock, fixture, (1201, 1202, 1203))
    assert len(events) == 1


async def test_list_recurring_service(hass, aioclient_mock, fixture, freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")
    entry = await _setup(hass, aioclient_mock, fixture)
    await _resync(hass, aioclient_mock, fixture, (1101, 1102, 1103))
    result = await hass.services.async_call(
        "ynab", "list_recurring", {"config_entry_id": entry.entry_id},
        blocking=True, return_response=True,
    )
    assert [r["key"] for r in result["recurring"]] == ["acc-savings|Mortgage Co|-650000"]
    assert entry.runtime_data.actions.list_recurring() == result["recurring"]
