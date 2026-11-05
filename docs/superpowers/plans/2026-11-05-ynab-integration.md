# YNAB for Home Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Home Assistant custom integration (`ynab`) that mirrors a YNAB plan as entities, fires events for new transactions, offers write actions, proposals and a waterfall allocator, ships blueprints, and serves the same capabilities to Claude as an HA LLM API.

**Architecture:** An async YNAB client feeds a `DataUpdateCoordinator` that keeps a delta-synced local copy of accounts, categories and recent transactions, persisted in an HA `Store`. Pure modules (`low`, `allocation`, `recurring`) hold the logic. A single `YnabActions` object implements every write, and both the HA services and the LLM tools call it. Entities read from the coordinator.

**Tech Stack:** Python 3.14, Home Assistant 2026.11, aiohttp, `probatio` schemas (as in `paprika-llm-api`), pytest + `pytest-homeassistant-custom-component` + `aioresponses`, managed with `uv`.

**Spec:** `docs/superpowers/specs/2026-11-05-ynab-design.md`

**Reference code:** `~/Documents/GitHub/paprika-llm-api` (MIT). Copy its patterns for the LLM API registration, `ToolAnnotations` presets, `_format_tool` schema tests, config entry runtime data and unload. **Never copy from `DeLuca21/ynab-ha`** (GPL-3.0).

## Global Constraints

- HA domain `ynab`. Repo `~/Documents/GitHub/ynab-homeassistant`. License MIT.
- YNAB API base `https://api.ynab.com/v1`, bearer personal access token, 200 requests/hour per token. Amounts are milliunits (`1000` = 1.00 in any currency).
- Endpoint paths: verify `plans` vs `budgets` against https://api.ynab.com before writing the client. Put the segment in one constant (`PLAN_SEGMENT`) so a rename is a one-line change.
- LLM tool names are prefixed `ynab__` (HA rejects unprefixed names from 2027.3).
- Event names: `ynab_transaction_imported`, `ynab_transaction_updated`, `ynab_proposal_created`, `ynab_recurring_detected`, `ynab_ready_to_assign_increased`. Event data never contains the token or raw API payloads.
- The token lives only in the config entry. Never log it. Redact it in diagnostics. Never put it in an exception message.
- Default poll interval 15 minutes (options: 5–60). Delta requests use `last_knowledge_of_server`.
- On HTTP 429, stop. Raise `YnabRateLimitError`, never retry inside the poll, and don't advance `server_knowledge`.
- `update_transaction` refuses an already-approved transaction unless `force: true`.
- Allocation is propose-only. `apply_allocation` needs a proposal id.
- Low-bucket default share: 20% of target. Can be overridden per category.
- Waterfall stage keys, in order: `bills`, `everyday`, `fun`, `savings`. Stage 0 (overspending) and overflow are implicit.
- Excluded from allocation and low sensors: the "Credit Card Payments" group, the internal "Inflow: Ready to Assign" category, and hidden/deleted categories.
- Packaging: `hacs.json` + `manifest.json`, like Paprika.

## Review Focus

1. **HA restart mid-month:** after a restart, `ynab_transaction_imported` must not fire again for transactions already seen. Seen ids are persisted. Test in Task 4.
2. **Deleted transactions in a delta** (`deleted: true`): remove them from local state, fire no imported event, and mark any proposal for them stale. Tests in Tasks 4 and 8.
3. **429 or a network error mid-poll:** the coordinator reports `UpdateFailed` with a readable message, entities go unavailable rather than showing stale-as-fresh, and `server_knowledge` stays put so the next poll catches up. Test in Task 4.
4. **Split transactions and transfers:** `update_transaction` refuses a category change on a split (it has subtransactions) or a transfer (`transfer_account_id` set), with a clear error, and doesn't send a broken PATCH. Test in Task 7.
5. **Token leakage:** diagnostics output, log records from a failing request, and `YnabAuthError` text don't contain the token. Tests in Tasks 1 and 5.

---

## File Structure

```
ynab-homeassistant/
  pyproject.toml            # pytest config + dev deps (uv)
  hacs.json  LICENSE  README.md  .gitignore
  custom_components/ynab/
    manifest.json  const.py  strings.json  translations/en.json
    client.py         # YnabClient + errors; HTTP only
    models.py         # dataclasses + JSON parsing + money helpers
    store.py          # YnabStore: persisted state (proposals, log, seen ids, knowledge, mapping…)
    coordinator.py    # YnabCoordinator: delta sync, events, low-state transitions
    low.py            # is_low() — pure
    allocation.py     # compute_allocation() — pure waterfall
    recurring.py      # detect_recurring() — pure
    actions.py        # YnabActions: every write; used by services + LLM tools
    services.py  services.yaml
    entity.py  sensor.py  binary_sensor.py
    config_flow.py    # user/plan steps + options flow
    diagnostics.py
    llm_api.py        # LLM API + tools
    __init__.py       # setup/unload, runtime data
  blueprints/automation/ynab/
    categorize_recurring.yaml  auto_categorize_small_deposits.yaml  notify_low_bucket.yaml
  tests/
    conftest.py  fixtures/*.json
    test_client.py test_models.py test_store.py test_coordinator.py test_config_flow.py
    test_entities.py test_low.py test_actions.py test_proposals.py test_allocation.py
    test_recurring.py test_llm_api.py test_blueprints.py test_diagnostics.py
```

---

### Task 1: Project scaffold and YNAB client

**Files:**
- Create: `pyproject.toml`, `.gitignore` (copy Paprika's), `LICENSE` (MIT, Ian Richardson 2026), `custom_components/ynab/manifest.json`, `const.py`, `client.py`, `tests/conftest.py`, `tests/test_client.py`

**Interfaces:**
- Produces:
  - `const.py`: `DOMAIN = "ynab"`, `API_BASE`, `PLAN_SEGMENT`, `TOOL_PREFIX = "ynab__"`, `DEFAULT_SCAN_MINUTES = 15`, `DEFAULT_LOW_SHARE = 0.20`, `STAGES = ("bills", "everyday", "fun", "savings")`, `EXCLUDED_GROUP_NAMES = {"Credit Card Payments", "Internal Master Category"}`, event name constants `EVENT_*`.
  - `client.py`:
    - Errors: `YnabError(Exception)`, `YnabAuthError(YnabError)` (401), `YnabRateLimitError(YnabError)` (429), `YnabNotFoundError(YnabError)` (404).
    - `class YnabClient(session: aiohttp.ClientSession, token: str)`:
      - `async def get_user() -> dict`
      - `async def list_plans() -> list[dict]`
      - `async def get_accounts(plan_id, since: int | None) -> tuple[list[dict], int]`
      - `async def get_categories(plan_id, since: int | None) -> tuple[list[dict], int]` (returns groups with nested categories)
      - `async def get_transactions(plan_id, since: int | None, since_date: date | None) -> tuple[list[dict], int]`
      - `async def get_month(plan_id, month: str) -> dict` (`month` = `"YYYY-MM-01"` or `"current"`)
      - `async def update_transactions(plan_id, updates: list[dict]) -> list[dict]` (bulk PATCH)
      - `async def set_category_budgeted(plan_id, month: str, category_id: str, budgeted_milliunits: int) -> dict`
      - `requests_last_hour: int` (property, rolling 3600 s window)

- [ ] **Step 1: Environment.** `uv venv --python 3.14 && uv pip install "pytest-homeassistant-custom-component" aioresponses pytest-asyncio`. `pyproject.toml` sets `asyncio_mode = "auto"`, `testpaths = ["tests"]`. `conftest.py` enables `enable_custom_integrations` autouse and loads JSON fixtures from `tests/fixtures/` (write `user.json`, `plans.json`, `accounts.json`, `categories.json`, `transactions.json`, `month.json` from the API docs' examples, including one split, one transfer, one deleted, one "Withdrawal" and one "Deposit" in an account named "Apple Savings", and a "Credit Card Payments" group).
- [ ] **Step 2: Write failing tests** in `tests/test_client.py`:
  - `test_sends_bearer_token`: request carries `Authorization: Bearer tok`.
  - `test_delta_passes_last_knowledge`: `get_transactions("p", since=42, since_date=None)` hits `…/transactions?last_knowledge_of_server=42` and returns `(list, server_knowledge)`.
  - `test_401_raises_auth_error_without_token`: raises `YnabAuthError`, and `"tok" not in str(err)`.
  - `test_429_raises_rate_limit_error`.
  - `test_failed_request_logs_no_token(caplog)`: on a 500, no log record contains the token.
  - `test_requests_last_hour_counts_and_expires` (freeze time: 3 calls → 3; +3601 s → 0).
  - `test_update_transactions_sends_bulk_patch`: body `{"transactions": [...]}`.
- [ ] **Step 3: Run** `uv run pytest tests/test_client.py -v`. Expected: FAIL (module missing).
- [ ] **Step 4: Implement `client.py`.** One private `_request(method, path, *, params=None, json=None) -> dict` that maps statuses to errors and returns `data`. Errors carry YNAB's `error.detail` text, never headers.
- [ ] **Step 5: Run tests.** Expected: PASS.
- [ ] **Step 6: Commit** `feat: scaffold integration and YNAB client`.

### Task 2: Models and money

**Files:** Create `custom_components/ynab/models.py`, `tests/test_models.py`

**Interfaces:**
- Produces (frozen dataclasses with `from_api(d: dict)` classmethods):
  - `Account(id, name, type, balance, cleared_balance, uncleared_balance, closed, deleted, on_budget)`. Money fields are `Decimal`.
  - `Category(id, group_id, group_name, name, hidden, deleted, budgeted, activity, balance, goal_type, goal_target, goal_under_funded, goal_target_date: date | None, order: int)`
  - `Transaction(id, date, amount, account_id, account_name, payee_name, import_payee_name, import_payee_name_original, category_id, category_name, memo, approved, cleared, deleted, transfer_account_id, is_split: bool)`
  - `def to_decimal(milliunits: int | None) -> Decimal | None`, `def to_milliunits(amount: Decimal | float | str) -> int` (round half-even to an integer).
  - `def direction(amount: Decimal) -> Literal["inflow", "outflow"]`

- [ ] **Step 1: Failing tests:** `to_milliunits("623.98") == 623980`; `to_decimal(-623980) == Decimal("-623.98")`; `Transaction.from_api` on the split fixture gives `is_split is True`; `Category.from_api` sets `goal_target_date` from `goal_target_month`/`goal_target_date` when present (verify the field name against the API docs); `order` is the index across groups in API order.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement `models.py`.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add YNAB models`.

### Task 3: Persistent store

**Files:** Create `custom_components/ynab/store.py`, `tests/test_store.py`

**Interfaces:**
- Produces `class YnabStore(hass, entry_id)`, key `f"ynab.{entry_id}"`, version 1:
  - `async def async_load() -> None` and `def schedule_save() -> None` (`async_delay_save`, 10 s), `async def async_save_now()`, `async def async_remove()`
  - Attributes (plain dicts/lists, JSON-safe): `knowledge: dict[str, int]` (keys `accounts`, `categories`, `transactions`), `seen_transaction_ids: set[str]` (serialized as a list; prune ids absent from the coordinator's window), `proposals: dict[str, dict]`, `allocations: dict[str, dict]`, `activity: list[dict]` (cap 500, oldest dropped), `low_state: dict[str, bool]`, `recurring_reported: set[str]`, `stage_mapping: {"groups": {gid: stage}, "categories": {cid: stage}, "overflow_category_id": str | None}`, `low_share_overrides: dict[str, float]`, `initialized: bool`.
  - `def log(kind: str, **fields) -> None` (appends `{"at": iso, "kind": kind, **fields}` and schedules a save).

- [ ] **Step 1: Failing tests** (with `hass`): round-trip all attributes through save/load; `activity` caps at 500; `log()` schedules a save; a missing file gives defaults with `initialized is False`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add persistent store`.

### Task 4: Coordinator: delta sync and events

**Files:** Create `custom_components/ynab/coordinator.py`, `custom_components/ynab/low.py`, `tests/test_coordinator.py`, `tests/test_low.py`

**Interfaces:**
- Consumes: `YnabClient`, models, `YnabStore`.
- Produces:
  - `low.py`: `def is_low(cat: Category, share: float) -> bool`: `True` if `cat.balance < 0`, or if `goal_target` is set, greater than 0, and `cat.balance < goal_target * share`. Always `False` for excluded groups (the caller filters).
  - `@dataclass YnabData(accounts: dict[str, Account], categories: dict[str, Category], transactions: dict[str, Transaction], ready_to_assign: Decimal, month: str, currency: str)`
  - `class YnabCoordinator(DataUpdateCoordinator[YnabData])`, constructed with `(hass, entry, client, store, plan_id)`:
    - `_async_update_data()`: delta-fetch accounts, categories and transactions (on the first-ever fetch, `since_date = today − 120 days`), then `get_month("current")`. Merge into the existing data (delete removed or deleted ids), prune transactions older than 120 days, update `store.knowledge` **only after every call succeeds**, then run `_after_sync(old, new)`.
    - `_after_sync`: (a) if `store.initialized` is True, fire `ynab_transaction_imported` for each non-deleted id not in `seen_transaction_ids`; then add all ids to the seen set and set `initialized = True`. (b) Fire `ynab_ready_to_assign_increased` with `{entry_id, old, new, delta}` when RTA rises (not on the first refresh). (c) Recompute `store.low_state` for eligible categories (the low binary sensors read it). (d) Hooks for proposals (Task 8) and recurring detection (Task 10): call `self.on_synced` callbacks with `(old, new)`.
    - Errors: `YnabAuthError` → `ConfigEntryAuthFailed`; other `YnabError`/`aiohttp.ClientError` → `UpdateFailed(str(err))`.
    - `def eligible_categories() -> list[Category]` (excludes hidden, deleted, `EXCLUDED_GROUP_NAMES`, the "Inflow: Ready to Assign" category).
    - `def low_share(category_id) -> float` (override, else the entry option, else `DEFAULT_LOW_SHARE`).
  - Imported event payload, all JSON-safe: `entry_id, transaction_id, account_id, account_name, import_payee_name, payee_name, category_id, category_name, amount (float, signed), direction, date (ISO), day (int), approved, cleared, memo`.

- [ ] **Step 1: Failing tests** (`aioresponses` for HTTP, `async_capture_events`):
  - `test_first_refresh_fires_no_imported_events` (`initialized` becomes True).
  - `test_new_transaction_fires_imported_event_with_payload`: second refresh with a delta adding a "Withdrawal" of −623.98 on day 3; assert the exact payload, including `direction == "outflow"`, `day == 3`, `amount == -623.98`.
  - `test_restart_does_not_refire` (Review Focus 1): build a new coordinator from a reloaded store, refresh with the same transactions, and assert no events.
  - `test_deleted_transaction_removed_without_event` (Review Focus 2).
  - `test_rate_limit_keeps_knowledge` (Review Focus 3): the categories call returns 429; `UpdateFailed` is raised and `store.knowledge` is unchanged.
  - `test_auth_error_triggers_reauth` → `ConfigEntryAuthFailed`.
  - `test_ready_to_assign_increase_event`: 100 → 1600 fires `{old: 100.0, new: 1600.0, delta: 1500.0}`; a decrease fires nothing.
  - `test_requests_per_poll_is_four`.
  - `test_low.py`: overspent → low; balance 19 against target 100 → low; 20 → not low; no target and positive balance → not low.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement `low.py` and `coordinator.py`.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add delta-sync coordinator and transaction events`.

### Task 5: Config flow, setup, diagnostics

**Files:** Create `config_flow.py`, `__init__.py`, `diagnostics.py`, `strings.json`, `translations/en.json`, `tests/test_config_flow.py`, `tests/test_diagnostics.py`

**Interfaces:**
- Consumes: client, store, coordinator.
- Produces:
  - Config flow: step `user` (`api_token`, a password `TextSelector`, validated with `get_user()`; `invalid_auth`/`cannot_connect` errors) → step `plan` (selector over `list_plans()`). The unique id is the plan id; title is the plan name; `entry.data = {"api_token", "plan_id"}`. Add a `reauth` step for a new token.
  - Options flow (menu): **general**: `scan_minutes` (5–60), `low_share` (0.05–0.9), `entity_accounts` and `entity_categories` (multi-selects; default all eligible). **stages**: one stage select (`bills`/`everyday`/`fun`/`savings`/none) per category group, plus `overflow_category_id`; writes `store.stage_mapping` (per-category overrides are set through the `set_stage_mapping` tool). **low overrides**: pick a category and set its share; writes `store.low_share_overrides`.
  - `@dataclass YnabRuntime(client, store, coordinator, actions)`; `type YnabConfigEntry = ConfigEntry[YnabRuntime]`. `actions` is filled in Task 7, so leave it `None` until then.
  - `async_setup_entry`: build the client with `async_get_clientsession(hass)`, load the store, first refresh, forward `sensor` and `binary_sensor`, and register the LLM API (Task 11). `async_unload_entry` saves the store immediately. `async_remove_entry` removes the store.
  - `diagnostics.py`: `async_get_config_entry_diagnostics` returns entry options, counts, knowledge, request count and the store with `api_token` redacted (`async_redact_data`).
- [ ] **Step 1: Failing tests:** happy path creates the entry with the right unique id; `invalid_auth` on 401; a duplicate plan aborts `already_configured`; the options flow saves values; reauth updates the token; `test_diagnostics_redacts_token` (Review Focus 5) asserts the token string appears nowhere in `json.dumps(diag)`.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add config flow, setup and diagnostics`.

### Task 6: Entities

**Files:** Create `entity.py`, `sensor.py`, `binary_sensor.py`, `tests/test_entities.py`

**Interfaces:**
- Consumes: `YnabCoordinator`, `low_state`, options.
- Produces:
  - Devices: plan device `(DOMAIN, plan_id)`; account devices `(DOMAIN, f"{plan_id}_{account_id}")` with `via_device` set to the plan device. Category entities sit on the plan device.
  - Unique ids: `f"{plan_id}_account_{id}"`, `f"{plan_id}_category_{id}"`, `f"{plan_id}_category_{id}_low"`, `f"{plan_id}_ready_to_assign"`, `f"{plan_id}_unapproved"`, `f"{plan_id}_proposals"`.
  - Account sensor: state is the balance. Attributes `cleared_balance`, `uncleared_balance`, `type`, `on_budget`.
  - Category sensor: state is available (`balance`). Attributes `assigned`, `activity`, `target`, `underfunded`, `target_date`, `group`, `stage` (from the mapping, or `None`).
  - Ready to Assign, unapproved count (transactions with `approved == False`), and pending proposals count (`len([p for p in store.proposals.values() if p["status"] == "pending"])`).
  - Monetary sensors: `device_class=MONETARY`, `state_class=TOTAL`, `native_unit_of_measurement` = the plan's ISO currency code.
  - Low binary sensor (`device_class=PROBLEM`) for each eligible category with a target or a negative balance. `is_on = store.low_state[cid]`. Attributes `category`, `available`, `target`, `share` (the blueprint's notification reads these).
  - New accounts or categories that appear on a later poll get entities added (listener on the coordinator). Only selected accounts and categories (options) get entities.
- [ ] **Step 1: Failing tests:** after setup with the fixtures, `sensor.<plan>_apple_savings` has the right state and unit; the category sensor's `underfunded` attribute is correct; no entities for hidden categories or the "Credit Card Payments" group; the low binary sensor goes off → on after a poll where the balance drops below 20% and stays on, with no state change on the next poll at the same balance; a category added in a delta gets an entity.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add sensors and low-bucket binary sensors`.

### Task 7: Write actions and services

**Files:** Create `actions.py`, `services.py`, `services.yaml`; modify `__init__.py` (create `YnabActions`, register services once in `async_setup`); create `tests/test_actions.py`

**Interfaces:**
- Consumes: client, coordinator, store.
- Produces `class YnabActions(hass, entry, client, coordinator, store)`:
  - `def resolve_category(ref: str) -> Category`: id match, else case-insensitive exact name among eligible categories. Raises `ServiceValidationError` when the name is unknown or ambiguous (and lists the candidates).
  - `async def update_transactions(updates: list[TxUpdate], *, source: str) -> list[str]`, where `TxUpdate = TypedDict(transaction_id, payee_name?, category?, memo?, approve?: bool, force?: bool)`:
    - Refuse (`ServiceValidationError`) an already-approved transaction without `force`, a category change on a split or transfer (Review Focus 4), and an unknown id.
    - Send one bulk PATCH, log `"transaction_updated"` to `store.activity` with the source and changes, fire `ynab_transaction_updated` per id, then `coordinator.async_request_refresh()`.
  - `async def assign_money(category: str, amount: Decimal, *, month: str = "current", mode: Literal["set", "add"] = "set", source: str) -> Decimal` (returns the new assigned amount).
  - `source` is `f"automation:{context.parent_id or context.id}"` or `"user:…"` / `"llm"`.
- Services (each takes `config_entry_id`, required, config-entry selector), with a schema for each in `services.yaml`:
  - `ynab.update_transaction` (single; fields as `TxUpdate`)
  - `ynab.update_transactions` (`updates: list`)
  - `ynab.assign_money` (`category`, `amount`, `month`, `mode`)
  - `ynab.refresh`
  - Services that return data use `SupportsResponse.OPTIONAL`.
- [ ] **Step 1: Failing tests:**
  - `test_update_sets_category_and_approves` (PATCH body has `category_id` and `approved: true`; event fired; activity logged).
  - `test_refuses_approved_without_force` / `test_force_allows_approved`.
  - `test_refuses_category_on_split` / `test_refuses_category_on_transfer` (no HTTP call made).
  - `test_resolve_category_ambiguous_lists_candidates`.
  - `test_assign_money_add_mode` (assigned 50 + add 25 → PUT/PATCH with 75000 milliunits).
  - `test_service_call_end_to_end` (`hass.services.async_call("ynab", "update_transaction", {...}, blocking=True)`).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add write actions and services`.

### Task 8: Proposals

**Files:** Modify `actions.py`, `services.py`, `services.yaml`; create `tests/test_proposals.py`

**Interfaces:**
- Produces on `YnabActions`:
  - `async def propose_transaction_update(update: TxUpdate, *, source: str) -> str`: validate as `update_transactions` would, without sending. Store `{"id": "p-" + 6 hex, "kind": "transaction", "transaction_id", "changes", "snapshot": {date, amount, account_name, import_payee_name, category_id}, "source", "created_at", "status": "pending"}` and fire `ynab_proposal_created` `{entry_id, proposal_id, kind, summary}`. If a pending proposal already exists for the same transaction, replace it and keep the same id.
  - `async def approve_proposals(ids: list[str], *, source) -> dict` (applies transaction proposals through `update_transactions` with `approve=True` unless the change says otherwise; allocation ids are routed to `apply_allocation`, Task 9). Returns `{"applied": [...], "skipped": {id: reason}}`.
  - `async def reject_proposals(ids, *, source) -> dict`
  - `def list_proposals(status: str = "pending") -> list[dict]`
  - A coordinator `on_synced` hook marks a pending transaction proposal `stale` when its transaction is deleted or approved, or its `category_id` differs from the snapshot.
- Services: `ynab.propose_transaction_update`, `ynab.approve_proposals`, `ynab.reject_proposals`, `ynab.list_proposals` (response only).
- [ ] **Step 1: Failing tests:** a propose call makes no HTTP write, stores a proposal and fires the event; a second proposal for the same transaction keeps the id; approve applies and sets status `approved`; approving a stale proposal is skipped with reason `"stale"`; a transaction approved in the YNAB app between polls makes the proposal stale; a deleted transaction makes it stale (Review Focus 2); the proposal count sensor updates.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add transaction proposals`.

### Task 9: Waterfall allocation

**Files:** Create `allocation.py`, `tests/test_allocation.py`; modify `actions.py`, `services.py`, `services.yaml`

**Interfaces:**
- Produces:
  - `@dataclass Move(month: str, category_id: str, category_name: str, amount: Decimal, stage: str, reason: str)`
  - `@dataclass AllocationResult(moves: list[Move], total: Decimal, leftover_to_overflow: Decimal, unfunded: list[tuple[str, Decimal]], unstaged: list[str])`
  - `def compute_allocation(available: Decimal, this_month: list[Category], next_month: list[Category], this_month_key: str, next_month_key: str, mapping: dict, overflow_category_id: str | None) -> AllocationResult`
  - On `YnabActions`: `async def propose_allocation(*, source) -> dict` (fetches `get_month` for the current and next month, which is 2 requests; stores `{"id": "a-…", "kind": "allocation", "moves", "total", "rta_at_creation", "status": "pending", …}`; fires `ynab_proposal_created`). `async def apply_allocation(proposal_id, *, source) -> dict` (re-fetches the current month; refuses with `"ready_to_assign_dropped"` when RTA < total; otherwise `assign_money(mode="add")` for each move and status `applied`).
- Algorithm (this is the logic the tests pin down):
  1. `stage(cat)` = `mapping["categories"].get(cat.id)` or `mapping["groups"].get(cat.group_id)`. Eligible categories only.
  2. Stage 0, this month: every category with `balance < 0` gets `-balance` (reason `"cover overspending"`), in category order, until the money runs out.
  3. For stage in `STAGES`, this month: categories in that stage with `goal_under_funded > 0`, sorted by `(goal_target_date or date.max, order)`, each get `min(underfunded, remaining)` (reason `"target"`). Partial funding is allowed for the last one.
  4. Repeat step 3 for next month, using next-month data. For balance-based goals (YNAB goal types `TB` and `TBD`), first subtract what this run already assigned to the same category this month.
  5. Remaining money goes to `overflow_category_id` this month (reason `"overflow"`). With no overflow configured, it stays in Ready to Assign (`leftover_to_overflow = 0` and it's reported).
  6. `unstaged` = names of eligible categories with an underfunded target and no stage. They get nothing.
  7. Zero or negative `available` → no moves.
- Services: `ynab.propose_allocation` and `ynab.apply_allocation` (`proposal_id`), both with response data.
- [ ] **Step 1: Failing tests** for `compute_allocation`:
  - `test_overspending_first`.
  - `test_stage_order_bills_before_everyday_before_fun_before_savings`.
  - `test_soonest_due_first_within_stage_and_partial_last`.
  - `test_late_month_paycheck_funds_next_month_before_overflow` (this month fully funded; 1500 available; next month's bills take 1200 and 300 goes to overflow).
  - `test_balance_goal_not_double_counted_next_month`.
  - `test_unstaged_reported_and_unfunded`.
  - `test_no_overflow_configured_leaves_money`.
  - `test_zero_available_no_moves`.
  - `test_excluded_groups_never_funded`.
  - Plus action tests: `test_apply_refuses_when_rta_dropped` and `test_apply_adds_each_move` (assert the PATCH amounts).
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: add waterfall allocation proposals`.

### Task 10: Recurring detection

**Files:** Create `recurring.py`, `tests/test_recurring.py`; modify `coordinator.py` (register the hook in `__init__`)

**Interfaces:**
- Produces:
  - `@dataclass Recurring(key: str, account_id, account_name, import_payee_name, amount: Decimal, typical_day: int, category_id, category_name, payee_name, occurrences: list[date])`
  - `def detect_recurring(transactions: Iterable[Transaction], today: date) -> list[Recurring]`. Consider approved, non-deleted, non-split, non-transfer transactions with a category from the last 3 calendar months. Group by `(account_id, import_payee_name or payee_name, amount)`. A group qualifies when it falls in ≥2 distinct months, all its days of the month are within ±3 of the median, and its latest occurrence uses one category. `key = f"{account_id}|{payee}|{milliunits}"`.
  - Hook: after each sync, fire `ynab_recurring_detected` (payload is the dataclass fields as JSON) for each key not in `store.recurring_reported`, then add the key. `YnabActions.list_recurring() -> list[dict]` returns the current detections.
- [ ] **Step 1: Failing tests:** mortgage on Sep 2, Oct 1 and Nov 3 → detected with `typical_day` 2; amounts that differ by one cent → not detected; days 1 and 15 → not detected; only one month → not detected; unapproved → ignored; event fires once across two syncs.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: detect recurring transactions`.

### Task 11: LLM API (MCP tools)

**Files:** Create `llm_api.py`, `tests/test_llm_api.py`; modify `__init__.py` (register with `entry.async_on_unload`)

**Interfaces:**
- Consumes: `YnabActions`, coordinator data, store.
- Produces: `class YnabLLMAPI(llm.API)` with a `runtime: YnabRuntime` field, and `def async_register(hass, entry) -> Callable`, with API id `f"ynab_{plan_id[:8]}"` and name `f"YNAB ({plan name})"`. Follow Paprika's `llm_api.py` for the tool base class, the error translation (`YnabRateLimitError` → a message telling the model nothing changed and not to retry) and the annotation presets.
- Tools (`ynab__` prefix; Paprika's annotation preset in brackets):
  - `get_summary` [read]: `{ready_to_assign, low: [{category, available, target}], pending_proposals, unapproved: [first 25 transactions], recurring: [...], activity_since: last 24 h}`
  - `list_accounts` [read]
  - `list_categories` [read] (optional `group`, `low_only`)
  - `list_transactions` [read] (`account`, `since`, `until`, `approved`, `limit` ≤ 200)
  - `update_transactions` [write destructive]
  - `assign_money` [write destructive]
  - `list_proposals` [read], `approve_proposals` [write destructive], `reject_proposals` [write]
  - `list_recurring_detections` [read]
  - `propose_allocation` [write additive], `apply_allocation` [write destructive]
  - `get_stage_mapping` [read], `set_stage_mapping` [write] (accepts group or category names, resolved like `resolve_category`; validates stage keys)
  - `get_activity_log` [read] (`limit` ≤ 100)
- API prompt (exact intent): show the user what will change and get a clear yes before any write, approve or apply tool; amounts are in the plan's currency; to automate a recurring item, suggest the "Categorize a recurring transaction" blueprint.
- [ ] **Step 1: Failing tests** (Paprika style, through `homeassistant.components.mcp_server.server._format_tool`): every tool's name starts with `ynab__`; every schema converts and every field has a description; read tools are annotated read-only; `get_summary` returns the expected keys from fixture data; `approve_proposals` calls `YnabActions.approve_proposals`; a rate-limit error becomes the "nothing changed" message.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Expected: PASS.
- [ ] **Step 5: Commit** `feat: expose YNAB tools as an HA LLM API`.

### Task 12: Blueprints, README, packaging

**Files:** Create `blueprints/automation/ynab/*.yaml` (3), `hacs.json`, `README.md`, `tests/test_blueprints.py`

**Interfaces:**
- Blueprints (all trigger on events; `entry_id` is taken from the event):
  - `categorize_recurring.yaml`: inputs `account_name`, `import_payee` (default "Withdrawal"), `direction` (inflow/outflow), `amount` (positive number), `day_from`/`day_to` (default 1–31), `payee_name` (optional), `category`, `mode` (`apply` | `propose`, default `propose`), `approve` (bool, default true). The trigger is `ynab_transaction_imported`. Conditions compare `trigger.event.data` fields; the amount check is `abs(amount) == input` to 2 dp. The action is `ynab.update_transaction` or `ynab.propose_transaction_update`, chosen by mode. The description warns that Apple-style feeds pre-fill a wrong category, so don't add a "category is empty" condition.
  - `auto_categorize_small_deposits.yaml`: inputs `account_name`, `import_payee` (default "Deposit"), `max_amount` (default 25), `category`, `approve` (default true). Inflow only, `amount < max_amount`.
  - `notify_low_bucket.yaml`: inputs `low_sensors` (binary_sensor entity selector, multiple, integration `ynab`), `notify_action` (action selector). Trigger: state `off` → `on`. The message names the category and its available amount, taken from the binary sensor's `category` and `available` attributes (Task 6).
- README: what it does, install (same packaging as Paprika, plus the HA marketplace note), getting a personal access token, entities, events (with payloads), actions, blueprints (with import links), the MCP setup (copy Paprika's section), the rate-limit note (sharing a token with other YNAB integrations shares the 200/hour), and Ian's examples: Apple Savings mortgage (propose) and cashback (auto).
- [ ] **Step 1: Failing tests:** each blueprint loads with HA's blueprint schema (`homeassistant.components.blueprint.models.Blueprint`); in a `hass` fixture, an automation built from `categorize_recurring` with the mortgage inputs, fed a synthetic `ynab_transaction_imported` event (Apple Savings, Withdrawal, −623.98, day 3), calls a mocked `ynab.propose_transaction_update` with category "Mortgage" and payee "Rocket Mortgage"; the same event on day 10 does not; the cashback blueprint applies to a 3.12 Deposit and not to 30.00.
- [ ] **Step 2: Run.** Expected: FAIL.
- [ ] **Step 3: Write the blueprints, `hacs.json` and the README.**
- [ ] **Step 4: Run the full suite** with `uv run pytest -v`. Expected: all PASS.
- [ ] **Step 5: Commit** `feat: add blueprints, README and packaging`.

### Task 13: Live smoke test (with Ian)

- [ ] **Step 1:** Ian installs the integration in his HA (copy `custom_components/ynab`, restart) and adds it with his token through the UI. **Claude never handles the token.**
- [ ] **Step 2:** Check the entities and the request count in diagnostics after an hour (≤ ~20).
- [ ] **Step 3:** Import both blueprints, and create the mortgage (propose) and cashback (auto) automations.
- [ ] **Step 4:** Enable the YNAB API in the MCP Server integration. In chat, call `ynab__get_summary` and `ynab__propose_allocation` (propose only, no apply), and review the result with Ian.
