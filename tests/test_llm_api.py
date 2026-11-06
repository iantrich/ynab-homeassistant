"""The YNAB LLM API, exercised through Home Assistant's own MCP formatter."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.mcp_server.server import _format_tool
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import llm
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ynab import llm_api
from custom_components.ynab.client import YnabRateLimitError
from tests.conftest import _register

EXPECTED = {
    "get_summary", "list_accounts", "list_categories", "list_transactions",
    "update_transactions", "assign_money", "list_proposals", "approve_proposals",
    "reject_proposals", "list_recurring_detections", "propose_allocation",
    "apply_allocation", "get_stage_mapping", "set_stage_mapping", "get_activity_log",
}
READ = {
    "get_summary", "list_accounts", "list_categories", "list_transactions",
    "list_proposals", "list_recurring_detections", "get_stage_mapping",
    "get_activity_log",
}


@pytest.fixture(autouse=True)
def frozen(freezer):
    freezer.move_to("2026-11-05T12:00:00+00:00")


async def _setup(hass, aioclient_mock, fixture):
    _register(aioclient_mock, fixture)
    entry = MockConfigEntry(
        domain="ynab",
        title="Household",
        data={"api_token": "tok", "plan_id": "p", "plan_name": "Household",
              "currency": "USD"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def tools(entry):
    return {
        t.name.removeprefix("ynab__"): t for t in llm_api._build_tools(entry.runtime_data)
    }


def _call(tool, **args):
    validated = tool.parameters(args)
    return tool.async_call(None, llm.ToolInput(tool_name=tool.name, tool_args=validated), None)


async def test_tool_set_and_schemas(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    built = llm_api._build_tools(entry.runtime_data)
    assert {t.name.removeprefix("ynab__") for t in built} == EXPECTED
    assert len(built) == len(EXPECTED)

    def walk(props, path):
        for key, spec in props.items():
            assert spec.get("description"), f"{path}{key} has no description"
            if spec.get("type") == "object":
                walk(spec.get("properties", {}), f"{path}{key}.")
            if spec.get("type") == "array" and spec["items"].get("type") == "object":
                walk(spec["items"].get("properties", {}), f"{path}{key}[].")

    for t in built:
        assert t.name.startswith("ynab__") and t.integration == "ynab"
        assert t.description and t.title
        spec = _format_tool(t, None)
        walk(spec.inputSchema.get("properties", {}), f"{t.name}.")


async def test_annotations(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    hints = {n: _format_tool(t, None).annotations for n, t in tools(entry).items()}
    for n in READ:
        assert hints[n].readOnlyHint is True and hints[n].destructiveHint is False, n
    for n in ("update_transactions", "assign_money", "approve_proposals", "apply_allocation"):
        assert hints[n].readOnlyHint is False and hints[n].destructiveHint is True, n
    assert hints["reject_proposals"].destructiveHint is False
    assert hints["set_stage_mapping"].readOnlyHint is False
    assert hints["propose_allocation"].destructiveHint is False


async def test_get_summary(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    entry.runtime_data.store.log("money_assigned", source="user")
    result = await _call(tools(entry)["get_summary"])
    data = result.data
    json.dumps(data)
    assert set(data) >= {
        "currency", "ready_to_assign", "low", "pending_proposals", "unapproved",
        "recurring", "activity_since",
    }
    assert data["currency"] == "USD"
    assert data["ready_to_assign"] == 1250.0
    assert {"t-withdrawal", "t-deposit"} <= {t["id"] for t in data["unapproved"]}
    assert len(data["activity_since"]) == 1
    assert all(set(x) == {"category", "available", "target"} for x in data["low"])


async def test_reads_are_json_safe(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    t = tools(entry)
    for name, args in (
        ("list_accounts", {}),
        ("list_categories", {}),
        ("list_categories", {"low_only": True}),
        ("list_transactions", {"approved": False, "limit": 5}),
        ("list_proposals", {}),
        ("list_recurring_detections", {}),
        ("get_stage_mapping", {}),
        ("get_activity_log", {"limit": 5}),
    ):
        json.dumps((await _call(t[name], **args)).data)
    txs = (await _call(t["list_transactions"], approved=False)).data["transactions"]
    assert txs and all(not x["approved"] for x in txs)


async def test_limit_is_bounded(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    with pytest.raises(Exception):
        tools(entry)["list_transactions"].parameters({"limit": 500})
    with pytest.raises(Exception):
        tools(entry)["get_activity_log"].parameters({"limit": 101})


async def test_approve_calls_actions_with_llm_source(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    with patch.object(
        type(entry.runtime_data.actions), "approve_proposals",
        AsyncMock(return_value={"applied": ["p-1"], "skipped": {}}),
    ) as mock:
        result = await _call(tools(entry)["approve_proposals"], ids=["p-1"])
    mock.assert_awaited_once_with(["p-1"], source="llm")
    assert result.data == {"applied": ["p-1"], "skipped": {}}


async def test_rate_limit_says_nothing_changed(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    with patch.object(
        type(entry.runtime_data.actions), "approve_proposals",
        AsyncMock(side_effect=YnabRateLimitError("429")),
    ), pytest.raises(HomeAssistantError, match="Nothing was changed.*do not retry"):
        await _call(tools(entry)["approve_proposals"], ids=["p-1"])
    wrapped = HomeAssistantError("YNAB rejected the update: 429")
    wrapped.__cause__ = YnabRateLimitError("429")
    with patch.object(
        type(entry.runtime_data.actions), "approve_proposals",
        AsyncMock(side_effect=wrapped),
    ), pytest.raises(HomeAssistantError, match="Nothing was changed"):
        await _call(tools(entry)["approve_proposals"], ids=["p-1"])


async def test_validation_error_becomes_tool_error(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    with pytest.raises(HomeAssistantError, match="Unknown transaction"):
        await _call(
            tools(entry)["update_transactions"],
            updates=[{"transaction_id": "nope", "memo": "x"}],
        )


async def test_assign_money_passes_decimal_and_llm_source(hass, aioclient_mock, fixture):
    from decimal import Decimal

    entry = await _setup(hass, aioclient_mock, fixture)
    with patch.object(
        type(entry.runtime_data.actions), "assign_money",
        AsyncMock(return_value=Decimal("12.5")),
    ) as mock:
        result = await _call(
            tools(entry)["assign_money"], category="Groceries", amount=12.5, mode="add"
        )
    mock.assert_awaited_once_with(
        "Groceries", Decimal("12.5"), month="current", mode="add", source="llm"
    )
    assert result.data["assigned"] == 12.5


async def test_set_stage_mapping(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    store = entry.runtime_data.store
    result = await _call(
        tools(entry)["set_stage_mapping"],
        groups={"bills": "bills"},
        categories={"GROCERIES": "everyday"},
        overflow_category="Groceries",
    )
    assert store.stage_mapping["groups"] == {"g-bills": "bills"}
    assert store.stage_mapping["categories"]["c-groceries"] == "everyday"
    assert store.stage_mapping["overflow_category_id"] == "c-groceries"
    assert result.data["stage_mapping"]["overflow_category"] == "Groceries"

    await _call(tools(entry)["set_stage_mapping"], groups={"Bills": "none"})
    assert "g-bills" not in store.stage_mapping["groups"]
    await _call(tools(entry)["set_stage_mapping"], overflow_category=None)
    assert store.stage_mapping["overflow_category_id"] is None


async def test_set_stage_mapping_rejects_bad_input(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    store = entry.runtime_data.store
    before = json.dumps(store.stage_mapping, sort_keys=True)
    t = tools(entry)["set_stage_mapping"]
    with pytest.raises(HomeAssistantError, match="stage"):
        await _call(t, groups={"Bills": "luxury"})
    with pytest.raises(HomeAssistantError, match="Unknown"):
        await _call(t, categories={"Nope": "bills"})
    assert json.dumps(store.stage_mapping, sort_keys=True) == before


async def test_prompt_and_registration(hass, aioclient_mock, fixture):
    entry = await _setup(hass, aioclient_mock, fixture)
    api_id = "ynab_p"
    instance = await llm.async_get_api(hass, api_id, llm.LLMContext(
        platform="test", context=None, language="en", assistant=None, device_id=None))
    assert instance.api.name == "YNAB (Household)"
    assert instance.api_prompt.startswith("Today is Thursday, 2026-11-05")
    assert "Categorize a recurring transaction" in instance.api_prompt
    assert len(instance.tools) == len(EXPECTED)

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    with pytest.raises(HomeAssistantError):
        await llm.async_get_api(hass, api_id, llm.LLMContext(
            platform="test", context=None, language="en", assistant=None, device_id=None))
