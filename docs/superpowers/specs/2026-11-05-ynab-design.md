# YNAB for Home Assistant — Design

Date: 2026-11-05
Owner: Ian Richardson
Repo: `~/Documents/GitHub/ynab-homeassistant` · HA domain `ynab` (Claude's calls)

## Intent

A general-purpose YNAB integration for Home Assistant that does more than show
balances: it can **fix transactions, apply rules, and propose how to assign
money**, and it exposes all of that to automations, to dashboards, and to
Claude through HA's MCP Server.

Ian's own reasons, which drove the design:

1. **Apple Savings imports are useless.** Every transaction arrives with the
   payee "Withdrawal" or "Deposit". YNAB remembers one category per payee, so
   each import gets whatever category was last used for that payee: often
   wrong, sometimes "Uncategorized". YNAB's own rules key on payee name only,
   never amount, so they can't tell his mortgage from anything else.
2. **Approving Apple Card cashback is tedious.** Each card purchase produces its
   own small "Deposit", and he has to approve them one by one every day.
3. **Assigning new money is manual.** When a paycheck lands he funds
   categories in a fixed priority order by hand.
4. **He wants to know when key buckets run low**, once, not repeatedly.

Ian uses HA for automations and notifications. Other users will want YNAB data
on dashboards as a "one-stop shop", so it ships as a shareable integration and
nothing Ian-specific is built in. His Apple Savings, mortgage and cashback
setups are **configuration (rules)**, not code.

### Why not contribute to `DeLuca21/ynab-ha`

Considered and rejected (Ian agreed). ynab-ha is read-only sensors only, with no
write actions, rules or LLM tools, so nearly everything here would be new code.
It has a single maintainer with slow reviews: open PRs for delta sync (Aug 2026)
and for token-logging (Oct 2026) have gone unanswered. It is GPL-3.0.
**Don't copy its code.** This repo is MIT like `paprika-llm-api`. Reading it for
ideas is fine.

## Goals

- Users can see YNAB state in HA: accounts, categories, Ready to Assign,
  pending approvals.
- Users can automate on YNAB: low-bucket alerts, new-transaction events, and
  actions that write to YNAB.
- Users categorize, and optionally approve, imported transactions with
  **HA's own automations**, matching on amount and timing as well as payee.
  The integration supplies events, actions and blueprints. There is no
  custom rules engine (Ian's call).
- A waterfall allocator proposes how to assign new money by priority stage
  and YNAB targets. Applying a proposal needs approval in v1.
- Claude gets the same data and actions as MCP tools, served by HA's MCP Server
  integration (same pattern as `paprika-llm-api`).

## Non-goals (v1)

- **Transfer/merchant matching** for bare "Withdrawal"s (pairing one with the
  matching transaction in another account). Ian deferred it. The proposal model
  must not preclude adding it later.
- **Automatic money assignment.** Propose only. Ian wants to "start with
  proposing to work out any kinks and go from there". Keep the change to
  automatic small: a per-entry option later.
- Real-time behaviour. Polling is fine.
- Calling the Claude API from HA. HA does deterministic work. Judgment happens
  when Claude is asked, in chat or in Ian's scheduled morning brief.
- Changes to Ian's morning-brief skill. That's a later step, after the
  integration works.
- Supporting more than one YNAB plan per config entry. Add another entry for
  another plan.

## Anti-goals (a failure even if it "works")

- Moving money or changing a transaction the user didn't approve, except
  through a rule the user explicitly set to **auto**.
- Touching a transaction that is already approved in YNAB. Manual edits win.
- Running over YNAB's 200 requests/hour limit.
- The access token appearing anywhere but HA's config entry store: not in
  logs, diagnostics, events, the repo or chat.
- Repeat low-bucket notifications while a bucket stays low.

## Constraints and facts

- YNAB API: `https://api.ynab.com/v1`, bearer personal access token,
  200 requests/hour per token, delta requests via `last_knowledge_of_server` /
  `server_knowledge`, no webhooks, amounts in milliunits. Recent API versions
  call budgets "plans". Confirm current endpoint names against the live docs.
- Useful transaction fields: `approved`, `payee_name`, `import_payee_name`,
  `import_payee_name_original`, `category_id`, `amount`, `date`, `account_id`,
  `deleted`.
- Useful category fields: `balance` (available), `budgeted` (assigned),
  `activity`, `goal_type`, `goal_target`, `goal_under_funded`,
  `goal_target_month`/due date, `hidden`, `deleted`. Month: `to_be_budgeted`
  (Ready to Assign).
- Tool names prefixed `ynab__`. HA stops accepting unprefixed names in 2027.3.
- Distribution: package like Paprika (`hacs.json`, manifest). HACS has been
  replaced by HA's built-in marketplace (HA 2026.11), and Ian says packaging the
  same way is fine.
- A user running ynab-ha on the same token shares the 200/hour limit. Say so
  in the README.

## What Ian decided

### Categorization: HA automations, not a rules engine

- The integration fires `ynab_transaction_imported` for each **newly
  imported** transaction. The event carries: entry id, transaction id, account
  id and name, raw import payee, payee, category, amount (as a decimal, with
  sign), direction, date, day of month, approved and cleared.
- Automations match it with ordinary HA triggers and conditions, then call:
  - `ynab.update_transaction`: set payee/category/memo and optionally
    approve. This is "auto".
  - `ynab.propose_transaction_update`: record a proposal and change nothing
    in YNAB. This is "propose", reviewed later.
- A category that's already filled in isn't trusted: Apple Savings imports
  arrive with YNAB's last-used category, so automations should overwrite it
  and not test for "Uncategorized". Blueprint descriptions say so.
- `ynab.update_transaction` refuses to change an already-approved
  transaction unless the call says `force: true`. Manual edits win.
- Ian's starting automations, built from the blueprints after install:
  - Mortgage: Apple Savings, "Withdrawal", outflow, exactly $623.98, days 1–5
    → payee "Rocket Mortgage", category "Mortgage". **propose**.
  - Cashback: Apple Savings, "Deposit", inflow, under $25 → category
    "Emergency Fund". **auto**, approved. (Cashback is typically under $10 and
    other deposits are much larger. The $25 cap was Claude's suggestion, and
    Ian accepted it.)
- Blueprints shipped:
  - **Categorize a recurring transaction:** account, raw payee, direction,
    exact amount, day window, payee, category, approve or propose.
  - **Auto-categorize small deposits:** account, raw payee, amount cap,
    category, approve.
  - **Notify when a bucket goes low:** categories, notify target.
- Rule suggestions: when a transaction in an account is categorized by hand
  and the same amount recurs around the same day each month, the integration
  fires `ynab_recurring_detected` and lists it in the summary, pointing at the
  recurring blueprint. Ian's ha-mcp setup lets Claude create the automation in
  chat.
- Because events fire only for transactions imported after setup, the
  first-install backlog is never touched automatically.
- Known trade-off: two automations may act on the same transaction. Last
  write wins, and both are logged.

### Allocation (assigning money)

- A fixed waterfall. Each stage is funded up to its YNAB targets before the
  next one starts:
  1. Cover overspending (every negative category back to zero).
  2. Required payments / bills.
  3. Everyday spending (groceries, gas, household…).
  4. Fun money.
  5. Savings.
  6. Anything left → the configured overflow category (Ian: Emergency Fund).
- When this month's targets are all funded, the waterfall carries on into
  **next month's** targets (stages 2–5 for next month) before anything goes to
  overflow. A paycheck on the 28th funds next month's bills.
- Propose only in v1. The user approves, then it's applied.

### Low buckets

- Alert only on the **transition from good to low**. No repeat while a bucket
  stays low. A bucket that recovers and drops again alerts again.

### Where review happens

- The integration does the deterministic work itself (rules, proposals,
  low-bucket state). Ian's scheduled morning brief can later report on it
  through the MCP tools. Building the integration and MCP tools comes first.

## Design (Claude's calls unless marked as Ian's above)

### HA structure

- **Config entry:** one per YNAB plan. The config flow asks for the token
  (password field, validated with a live call) and then the plan. The options
  flow covers: poll interval, which accounts and categories get entities
  (hidden/closed/deleted excluded by default), the stage mapping, the overflow
  category and the low-bucket rule.
- **Devices:** one "plan" device (Ready to Assign, pending approvals,
  proposals) and one device per account. Categories hang off the plan device.
  Mirror HA's own conventions.
- **Entities:**
  - `sensor` per account: balance, with cleared/uncleared as attributes.
  - `sensor` per category: available, with attributes for assigned, activity,
    target, underfunded, due date and stage.
  - `sensor` Ready to Assign; `sensor` count of unapproved transactions;
    `sensor` count of pending proposals.
  - `binary_sensor` "low" per category with a target. An automation triggering
    on `off → on` gives transition-only alerts natively.
  - All monetary sensors use `device_class: monetary` with the plan's
    currency, so statistics and the energy-style cards work.
- **Events** (`ynab_` prefix, no secrets or raw payloads):
  `ynab_transaction_imported`, `ynab_transaction_updated` (by an action),
  `ynab_proposal_created`, `ynab_recurring_detected`,
  `ynab_ready_to_assign_increased` (old, new, delta: a trigger for running
  the allocator).
- **Actions (services):** `update_transaction`, `update_transactions`
  (bulk), `propose_transaction_update`, `assign_money` (set or add to a
  category's assigned amount for a month), `approve_proposals`,
  `reject_proposals`, `propose_allocation`, `apply_allocation`, `refresh`.
  Usable from automations and scripts. Actions that return data use HA
  response data.
- **Blueprints:** see Categorization above.

### Low-bucket definition

A category is low when it's overspent, or when it has a target and its
available balance is below a configurable share of that target (default 20%).
The share can be overridden per category in the options flow. Kept simple and
predictable so the binary sensor doesn't flap; the builder may add
hysteresis.

### Data layer

- **Client:** thin async aiohttp client, rate-limit aware. It counts requests
  in the rolling hour and reads YNAB's rate-limit header if one is present. On
  429 it stops and reports rather than retrying. It never logs the token or
  raw payloads.
- **Coordinator:** a `DataUpdateCoordinator`, default every 15 minutes, using
  delta requests for transactions, categories and accounts, plus the current
  month for Ready to Assign. The baseline is about 16 requests/hour. Writes
  trigger a refresh. The first poll is a full fetch.
- **Import detection:** after each delta poll, transactions not seen before
  (by id) fire `ynab_transaction_imported`. The first poll after install only
  records ids and fires nothing.
- **Store:** HA `Store` per entry holding pending proposals (stable ids,
  created-at, source automation/context, and stale once the transaction is
  approved or changed elsewhere), recurring detections, an activity log (what
  each action changed, when, and from which automation or user context),
  `server_knowledge`, seen transaction ids, low-bucket state and the stage
  mapping.

### Stage mapping

Each waterfall stage (bills, everyday spending, fun money, savings) maps to
YNAB **category groups**, with individual categories able to override their
group's stage. A category with no stage gets nothing from the allocator and
is listed as "unstaged" in the proposal, so a forgotten group is visible
rather than silently skipped. Within a stage, when money runs out, fund the
category whose target is due soonest; ties go by YNAB's category order.

### Allocation proposal

Computed on demand, not on every poll. Read Ready to Assign and category state
for this month and next, walk the waterfall, and return the moves
`(month, category, amount, stage, reason)`, plus what's left unfunded and any
unstaged categories. Applying takes the proposal id. It re-checks that Ready to
Assign hasn't dropped below the total, refusing and asking for a fresh
proposal if it has, then sets each category's assigned amount for its month.

### MCP tools (prefix `ynab__`)

Registered as an HA LLM API, exactly as in `paprika-llm-api`. They're thin
wrappers over the same coordinator data and actions.

| Tool | Kind |
|---|---|
| `get_summary` | read: Ready to Assign, low buckets, pending proposals, unapproved transactions, recurring detections, overnight activity. One call for a brief |
| `list_accounts`, `list_categories`, `list_transactions` | read, with filters |
| `update_transactions` | write: payee/category/memo/approved, one or many |
| `assign_money` | write |
| `list_proposals`, `approve_proposals`, `reject_proposals` | proposals |
| `list_recurring_detections` | read: candidates for the recurring blueprint |
| `propose_allocation`, `apply_allocation` | waterfall. Apply needs a proposal id |
| `get_stage_mapping`, `set_stage_mapping` | config |
| `get_activity_log` | read |

The API prompt and tool descriptions tell Claude to show proposals to the user
and get a yes before calling any approve, apply or write tool.

## Left to the builder

- Module layout (follow `paprika-llm-api` and HA conventions), and tests
  (pytest with mocked YNAB responses, as Paprika does).
- Proposal ids and schema.
- How recurrence is detected (e.g. same amount, day ±3, in ≥2 of the last 3
  months).
- Poll interval bounds, backoff, and hysteresis on the low sensor.
- Entity naming and unique ids. Diagnostics, with the token redacted.
- Build order. Suggested: client and coordinator with read entities → events
  and write actions → proposals → allocation → MCP tools → blueprints.
