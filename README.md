# YNAB — Home Assistant

A Home Assistant custom integration for [YNAB](https://www.ynab.com/) (You Need
A Budget). It shows your budget as sensors, fires events when transactions
arrive, lets automations and LLM clients such as Claude categorize them, and
helps you spread Ready to Assign across your categories.

It also registers a Home Assistant **LLM API**, which Home Assistant's
[Model Context Protocol Server](https://www.home-assistant.io/integrations/mcp_server/)
integration serves to MCP clients, with sign-in handled by Home Assistant.

## What you can do

- **See your budget:** Ready to Assign, unapproved transactions, pending
  proposals, account balances and category balances as sensors.
- **Know when a category runs low:** a problem sensor per category turns on
  when its available amount drops below a share of its target (20% by
  default).
- **Automate imports:** every newly imported transaction fires an event, so a
  blueprint can categorize the mortgage or the daily cashback for you.
- **Review before anything changes:** actions can *propose* a change. Nothing
  is written to YNAB until you approve the proposal.
- **Spread Ready to Assign:** propose an allocation across bills, everyday,
  fun and savings, review it, then apply it.
- **Ask an assistant:** Claude can read the budget, suggest moves and, after
  your yes, make them.

## Requirements

- Home Assistant 2026.10 or newer
- A YNAB account and a personal access token
- Optional: the Model Context Protocol Server integration, for MCP clients

## Installation

This integration is distributed the same way as
Paprika LLM API: as a custom
repository or by copying files.

1. Add this repository as a custom integration repository and download it, or
   copy `custom_components/ynab` into `config/custom_components/`.
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → YNAB**, and paste your
   personal access token.
4. Pick the budget (YNAB calls it a plan), then the accounts and categories you
   want entities for.

Note: Home Assistant 2026.11 replaced HACS with its built-in marketplace.
Whether you add the repository there or copy the folder by hand, the integration
is the same.

### Getting a personal access token

1. Sign in at [app.ynab.com](https://app.ynab.com/).
2. Open **Account Settings → Developer Settings**.
3. Under **Personal Access Tokens**, choose **New Token**, enter your password
   and copy the token. YNAB shows it once.
4. Paste it into the integration's setup form. It is stored in the Home
   Assistant config entry and is never returned by an action, event or tool.

Never put a token in a blueprint, an automation or an issue report.

### Rate limit

YNAB allows 200 requests per hour per token. The integration polls every 15
minutes by default (5 to 60 in the options) and uses delta requests, so it
uses very few. If you share one token with other YNAB integrations or scripts,
they all draw from the same 200. Create a separate token for each if you can.

## Entities

| Entity | Notes |
|---|---|
| Ready to Assign | sensor, in the plan's currency |
| Unapproved transactions | sensor, a count |
| Pending proposals | sensor, a count |
| One sensor per chosen account | the account balance |
| One sensor per chosen category | the available balance |
| `<Category> low` | binary sensor, on when the category is low; attributes `category`, `available`, `target`, `share` |

## Events

Events are fired on the Home Assistant bus; use them as automation triggers.
Every payload includes `entry_id`.

| Event | Fired when |
|---|---|
| `ynab_transaction_imported` | a new transaction arrives from a bank import (hand-entered and scheduled transactions do not fire it) |
| `ynab_transaction_updated` | a transaction changes |
| `ynab_proposal_created` | a proposal is recorded |
| `ynab_recurring_detected` | a transaction is seen recurring monthly |
| `ynab_ready_to_assign_increased` | Ready to Assign goes up |

`ynab_transaction_imported` payload:

| Field | Meaning |
|---|---|
| `entry_id` | the config entry to pass to actions |
| `transaction_id` | the YNAB transaction id |
| `account_id`, `account_name` | the account |
| `import_payee_name` | the payee as imported from the bank, for example `Withdrawal` |
| `payee_name` | the payee currently set in YNAB |
| `category_id`, `category_name` | the category currently set |
| `amount` | a signed number; outflows are negative |
| `direction` | `inflow` or `outflow` |
| `date`, `day` | the date and its day of the month |
| `approved`, `cleared`, `memo` | the transaction's state |

## Actions

Every action takes a required `config_entry_id`. Wherever an action takes a
`category`, it accepts a name or an id.

| Action | What it does |
|---|---|
| `ynab.update_transaction` | change a transaction's `category`, `payee_name` or `memo`; `approve` approves it; `force` allows changing one already approved |
| `ynab.update_transactions` | the same for a list of updates |
| `ynab.propose_transaction_update` | record the same change as a proposal; `approve` says whether approving it also approves the transaction |
| `ynab.assign_money` | set or add to a category's assigned amount for a month |
| `ynab.approve_proposals` | apply pending proposals to YNAB |
| `ynab.reject_proposals` | dismiss pending proposals |
| `ynab.list_proposals` | return proposals (response only) |
| `ynab.propose_allocation` | propose how to spread Ready to Assign |
| `ynab.apply_allocation` | apply an allocation proposal |
| `ynab.list_recurring` | return monthly recurring transactions (response only) |
| `ynab.refresh` | poll YNAB now |

## Blueprints

| Blueprint | Import |
|---|---|
| Categorize a recurring transaction | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fiantrich%2Fynab-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fynab%2Fcategorize_recurring.yaml) |
| Auto-categorize small deposits | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fiantrich%2Fynab-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fynab%2Fauto_categorize_small_deposits.yaml) |
| Notify when a category runs low | [![Import](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fiantrich%2Fynab-homeassistant%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fynab%2Fnotify_low_bucket.yaml) |

The two categorizing blueprints run in `queued` mode, because many
transactions can arrive in one poll. Account and payee names are matched
without regard to case, and amounts are compared to two decimal places.

### Example: an Apple Savings account

Apple Savings feeds into YNAB with generic payees (`Withdrawal`, `Deposit`)
and often pre-fills a category that is wrong. For that reason the blueprints
never require the category to be empty, and you should not add such a
condition to your own automations.

**Mortgage** (a proposal you approve): use *Categorize a recurring
transaction* with

| Input | Value |
|---|---|
| Account name | Apple Savings |
| Import payee | Withdrawal |
| Direction | outflow |
| Amount | 623.98 |
| Days | 1 to 5 |
| New payee name | Rocket Mortgage |
| Category | Mortgage |
| Mode | propose |

When the withdrawal lands, a proposal appears. Approve it with
`ynab.approve_proposals`, or ask Claude to.

**Cashback** (applied automatically): use *Auto-categorize small deposits*
with

| Input | Value |
|---|---|
| Account name | Apple Savings |
| Import payee | Deposit |
| Maximum amount | 25 |
| Category | Emergency Fund |

Any deposit under 25 is categorized to Emergency Fund and approved.

## MCP setup

The integration registers one LLM API per budget, named `ynab_` followed by the
first eight characters of the plan id. Home Assistant's Model Context Protocol
Server integration serves it, so you do not run anything extra.

1. Add **Model Context Protocol Server** in **Settings → Devices & Services**.
2. Make Home Assistant reachable by your client, for example with Home
   Assistant Cloud (Nabu Casa) or your own setup.
3. Connect your MCP client to `https://<your-home-assistant>/api/mcp/ynab_<plan id prefix>`
   and sign in with a Home Assistant administrator account. The exact address
   is the LLM API's id, shown in the Model Context Protocol Server's list of
   APIs.

Tools that change YNAB tell the model to show you exactly what will change and
get a clear yes first. Reading and `propose_allocation` change nothing.

## Development

```bash
uv run pytest
```

Tests use Home Assistant's `hass` and `aioclient_mock` fixtures from
`pytest-homeassistant-custom-component`.

## License

See `LICENSE`.
