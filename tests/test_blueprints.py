"""Blueprint tests: schema loading and behaviour on synthetic events."""

from pathlib import Path
import shutil

import pytest
from homeassistant.components.blueprint import models
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.ynab.const import EVENT_TRANSACTION_IMPORTED

ROOT = Path(__file__).parent.parent / "blueprints" / "automation" / "ynab"
BLUEPRINTS = ["categorize_recurring", "auto_categorize_small_deposits", "notify_low_bucket"]
ENTRY_ID = "entry-1"

MORTGAGE_INPUTS = {
    "account_name": "Apple Savings",
    "import_payee": "Withdrawal",
    "direction": "outflow",
    "amount": 623.98,
    "day_from": 1,
    "day_to": 5,
    "payee_name": "Rocket Mortgage",
    "category": "Mortgage",
    "mode": "propose",
}
CASHBACK_INPUTS = {
    "account_name": "Apple Savings",
    "import_payee": "Deposit",
    "max_amount": 25,
    "category": "Emergency Fund",
}


def _event(amount: float, day: int, **overrides) -> dict:
    return {
        "entry_id": ENTRY_ID,
        "transaction_id": "tx-1",
        "account_id": "acc-1",
        "account_name": "Apple Savings",
        "import_payee_name": "Withdrawal" if amount < 0 else "Deposit",
        "payee_name": None,
        "category_id": "cat-wrong",
        "category_name": "Some Prefilled Category",
        "amount": amount,
        "direction": "outflow" if amount < 0 else "inflow",
        "date": f"2026-11-{day:02d}",
        "day": day,
        "approved": False,
        "cleared": "cleared",
        "memo": None,
        **overrides,
    }


async def _setup(hass: HomeAssistant, name: str, inputs: dict) -> None:
    dest = Path(hass.config.path("blueprints/automation/ynab"))
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / f"{name}.yaml", dest / f"{name}.yaml")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "alias": name,
                "use_blueprint": {"path": f"ynab/{name}.yaml", "input": inputs},
            }
        },
    )
    await hass.async_block_till_done()


async def _fire(hass: HomeAssistant, data: dict) -> None:
    hass.bus.async_fire(EVENT_TRANSACTION_IMPORTED, data)
    await hass.async_block_till_done()


@pytest.mark.parametrize("name", BLUEPRINTS)
def test_blueprint_loads(hass: HomeAssistant, name: str) -> None:
    """Each blueprint passes Home Assistant's blueprint schema."""
    blueprint = models.Blueprint(
        load_yaml_dict(ROOT / f"{name}.yaml"),
        expected_domain="automation",
        path=f"ynab/{name}.yaml",
        schema=BLUEPRINT_SCHEMA,
    )
    assert blueprint.domain == "automation"
    assert blueprint.inputs


async def test_mortgage_proposed(hass: HomeAssistant) -> None:
    propose = async_mock_service(hass, "ynab", "propose_transaction_update")
    update = async_mock_service(hass, "ynab", "update_transaction")
    await _setup(hass, "categorize_recurring", MORTGAGE_INPUTS)

    await _fire(hass, _event(-623.98, 3))

    assert update == []
    assert len(propose) == 1
    data = propose[0].data
    assert data["config_entry_id"] == ENTRY_ID
    assert data["transaction_id"] == "tx-1"
    assert data["category"] == "Mortgage"
    assert data["payee_name"] == "Rocket Mortgage"
    assert data["approve"] is True


async def test_mortgage_apply_mode(hass: HomeAssistant) -> None:
    propose = async_mock_service(hass, "ynab", "propose_transaction_update")
    update = async_mock_service(hass, "ynab", "update_transaction")
    await _setup(hass, "categorize_recurring", {**MORTGAGE_INPUTS, "mode": "apply"})

    await _fire(hass, _event(-623.98, 3))

    assert propose == []
    assert len(update) == 1
    assert update[0].data["category"] == "Mortgage"


@pytest.mark.parametrize(
    "event",
    [
        _event(-623.98, 10),
        _event(-623.98, 6),
        _event(-623.97, 3),
        _event(623.98, 3),
        _event(-623.98, 3, account_name="Checking"),
        _event(-623.98, 3, import_payee_name="Something else"),
    ],
    ids=["day10", "day6", "amount", "inflow", "account", "import_payee"],
)
async def test_mortgage_not_matched(hass: HomeAssistant, event: dict) -> None:
    propose = async_mock_service(hass, "ynab", "propose_transaction_update")
    await _setup(hass, "categorize_recurring", MORTGAGE_INPUTS)
    await _fire(hass, event)
    assert propose == []


async def test_mortgage_boundaries_and_case(hass: HomeAssistant) -> None:
    propose = async_mock_service(hass, "ynab", "propose_transaction_update")
    await _setup(hass, "categorize_recurring", MORTGAGE_INPUTS)
    await _fire(hass, _event(-623.98, 1, account_name="APPLE savings"))
    await _fire(hass, _event(-623.98, 5, import_payee_name="withdrawal"))
    assert len(propose) == 2


async def test_payee_optional(hass: HomeAssistant) -> None:
    propose = async_mock_service(hass, "ynab", "propose_transaction_update")
    inputs = {k: v for k, v in MORTGAGE_INPUTS.items() if k != "payee_name"}
    await _setup(hass, "categorize_recurring", inputs)
    await _fire(hass, _event(-623.98, 3))
    assert len(propose) == 1
    assert "payee_name" not in propose[0].data


async def test_prefilled_category_still_matches(hass: HomeAssistant) -> None:
    """Apple-style feeds pre-fill a wrong category, so it must not be a condition."""
    propose = async_mock_service(hass, "ynab", "propose_transaction_update")
    await _setup(hass, "categorize_recurring", MORTGAGE_INPUTS)
    await _fire(hass, _event(-623.98, 3, category_id="c", category_name="Dining Out"))
    assert len(propose) == 1


async def test_cashback_applied(hass: HomeAssistant) -> None:
    update = async_mock_service(hass, "ynab", "update_transaction")
    await _setup(hass, "auto_categorize_small_deposits", CASHBACK_INPUTS)

    await _fire(hass, _event(3.12, 7))

    assert len(update) == 1
    assert update[0].data["category"] == "Emergency Fund"
    assert update[0].data["transaction_id"] == "tx-1"
    assert update[0].data["config_entry_id"] == ENTRY_ID
    assert update[0].data["approve"] is True


@pytest.mark.parametrize(
    "event",
    [
        _event(30.0, 7),
        _event(25.0, 7),
        _event(-3.12, 7, import_payee_name="Deposit"),
        _event(3.12, 7, account_name="Checking"),
        _event(3.12, 7, import_payee_name="Interest"),
    ],
    ids=["30", "equal-max", "outflow", "account", "import_payee"],
)
async def test_cashback_not_matched(hass: HomeAssistant, event: dict) -> None:
    update = async_mock_service(hass, "ynab", "update_transaction")
    await _setup(hass, "auto_categorize_small_deposits", CASHBACK_INPUTS)
    await _fire(hass, event)
    assert update == []


async def test_notify_low_bucket(hass: HomeAssistant) -> None:
    calls = async_mock_service(hass, "test", "notify")
    entity = "binary_sensor.groceries_low"
    hass.states.async_set(
        entity, "off", {"category": "Groceries", "available": 12.5}
    )
    await _setup(
        hass,
        "notify_low_bucket",
        {
            "low_sensors": [entity],
            "notify_action": [
                {
                    "action": "test.notify",
                    "data": {"message": "{{ message }}"},
                }
            ],
        },
    )

    hass.states.async_set(
        entity, "on", {"category": "Groceries", "available": 12.5}
    )
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert "Groceries" in calls[0].data["message"]
    assert "12.50" in calls[0].data["message"]


def test_services_yaml_names_and_descriptions():
    import yaml
    from pathlib import Path

    services = yaml.safe_load(
        (Path(__file__).parents[1] / "custom_components/ynab/services.yaml").read_text()
    )
    for svc, spec in services.items():
        assert spec.get("name") and spec.get("description"), svc
        for field, fspec in spec.get("fields", {}).items():
            assert fspec.get("name") and fspec.get("description"), f"{svc}.{field}"
