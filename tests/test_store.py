"""Tests for the persistent store."""

from unittest.mock import patch

from custom_components.ynab.store import ACTIVITY_CAP, YnabStore


async def test_missing_file_gives_defaults(hass):
    store = YnabStore(hass, "e1")
    await store.async_load()
    assert store.initialized is False
    assert store.knowledge == {}
    assert store.seen_transaction_ids == set()
    assert store.proposals == {}
    assert store.allocations == {}
    assert store.activity == []
    assert store.low_state == {}
    assert store.recurring_reported == set()
    assert store.stage_mapping == {
        "groups": {},
        "categories": {},
        "overflow_category_id": None,
    }
    assert store.low_share_overrides == {}


async def test_round_trip(hass, hass_storage):
    store = YnabStore(hass, "e1")
    await store.async_load()
    store.knowledge = {"accounts": 1, "categories": 2, "transactions": 3}
    store.seen_transaction_ids = {"t1", "t2"}
    store.proposals = {"p1": {"status": "pending"}}
    store.allocations = {"a1": {"total": 5}}
    store.log("something", detail="x")
    store.low_state = {"c1": True}
    store.recurring_reported = {"payee-a"}
    store.stage_mapping = {
        "groups": {"g1": "bills"},
        "categories": {"c1": "fun"},
        "overflow_category_id": "c9",
    }
    store.low_share_overrides = {"c1": 0.5}
    store.initialized = True
    await store.async_save_now()

    assert hass_storage["ynab.e1"]["version"] == 1
    assert isinstance(hass_storage["ynab.e1"]["data"]["seen_transaction_ids"], list)

    other = YnabStore(hass, "e1")
    await other.async_load()
    assert other.knowledge == store.knowledge
    assert other.seen_transaction_ids == {"t1", "t2"}
    assert other.proposals == store.proposals
    assert other.allocations == store.allocations
    assert other.activity == store.activity
    assert other.activity[0]["kind"] == "something"
    assert other.activity[0]["detail"] == "x"
    assert "at" in other.activity[0]
    assert other.low_state == {"c1": True}
    assert other.recurring_reported == {"payee-a"}
    assert other.stage_mapping == store.stage_mapping
    assert other.low_share_overrides == {"c1": 0.5}
    assert other.initialized is True


async def test_activity_caps_at_500(hass):
    store = YnabStore(hass, "e1")
    for i in range(ACTIVITY_CAP + 25):
        store.log("k", n=i)
    assert len(store.activity) == ACTIVITY_CAP
    assert store.activity[0]["n"] == 25
    assert store.activity[-1]["n"] == ACTIVITY_CAP + 24


async def test_log_schedules_save(hass):
    store = YnabStore(hass, "e1")
    with patch.object(store._store, "async_delay_save") as delay:
        store.log("k")
        delay.assert_called_once()
        assert delay.call_args.args[1] == 10


async def test_prune_seen(hass):
    store = YnabStore(hass, "e1")
    store.seen_transaction_ids = {"a", "b", "c"}
    store.prune_seen(["b", "c", "z"])
    assert store.seen_transaction_ids == {"b", "c"}


async def test_remove(hass, hass_storage):
    store = YnabStore(hass, "e1")
    await store.async_save_now()
    assert "ynab.e1" in hass_storage
    await store.async_remove()
    assert "ynab.e1" not in hass_storage
