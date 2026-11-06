"""Delta-sync coordinator for the YNAB integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
import logging
from decimal import Decimal

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .client import YnabAuthError, YnabClient, YnabError
from .const import (
    DEFAULT_LOW_SHARE,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    EVENT_READY_TO_ASSIGN_INCREASED,
    EVENT_TRANSACTION_IMPORTED,
    EXCLUDED_GROUP_NAMES,
)
from .low import is_low
from .models import (
    Account,
    Category,
    Transaction,
    categories_from_groups,
    direction,
    to_decimal,
)
from .store import YnabStore

_LOGGER = logging.getLogger(__name__)

WINDOW_DAYS = 120
RTA_CATEGORY_NAME = "Inflow: Ready to Assign"

SyncListener = Callable[["YnabData | None", "YnabData"], None]


@dataclass
class YnabData:
    """Local snapshot of the plan."""

    accounts: dict[str, Account]
    categories: dict[str, Category]
    transactions: dict[str, Transaction]
    ready_to_assign: Decimal
    month: str
    currency: str


class YnabCoordinator(DataUpdateCoordinator[YnabData]):
    """Polls YNAB with delta requests and fires events for new activity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: YnabClient,
        store: YnabStore,
        plan_id: str,
        currency: str,
    ) -> None:
        minutes = entry.options.get("scan_minutes", DEFAULT_SCAN_MINUTES)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.store = store
        self.plan_id = plan_id
        self.currency = currency
        self.on_synced: list[SyncListener] = []
        # Local state is not persisted, so the first refresh of every
        # instance is a full fetch regardless of stored knowledge.
        self._synced_once = False

    def add_sync_listener(self, cb: SyncListener) -> Callable[[], None]:
        """Register a callback run after each sync; returns an unsubscribe."""
        self.on_synced.append(cb)

        def _remove() -> None:
            if cb in self.on_synced:
                self.on_synced.remove(cb)

        return _remove

    # -- helpers used by entities -------------------------------------------------

    def eligible_categories(self) -> list[Category]:
        """Categories that count for low sensors and allocation."""
        if self.data is None:
            return []
        return _eligible(self.data.categories)

    def low_share(self, category_id: str) -> float:
        """Share of target below which a category is low."""
        override = self.store.low_share_overrides.get(category_id)
        if override is not None:
            return override
        option = self.config_entry.options.get("low_share")
        return option if option is not None else DEFAULT_LOW_SHARE

    # -- sync ---------------------------------------------------------------------

    async def _async_update_data(self) -> YnabData:
        knowledge = self.store.knowledge
        full = not self._synced_once
        since = {} if full else knowledge
        since_date = (
            dt_util.now().date() - timedelta(days=WINDOW_DAYS) if full else None
        )
        try:
            raw_accounts, k_accounts = await self.client.get_accounts(
                self.plan_id, since.get("accounts"), poll=True
            )
            raw_groups, k_categories = await self.client.get_categories(
                self.plan_id, since.get("categories"), poll=True
            )
            raw_txs, k_transactions = await self.client.get_transactions(
                self.plan_id, since.get("transactions"), since_date, poll=True
            )
            month = await self.client.get_month(self.plan_id, "current", poll=True)
        except YnabAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (YnabError, aiohttp.ClientError) as err:
            raise UpdateFailed(str(err)) from err

        old = None if full else self.data
        new = self._merge(old, raw_accounts, raw_groups, raw_txs, month)

        # Commit the merged snapshot first, then advance knowledge, so a
        # failure in _after_sync can never lose a delta.
        self.data = new
        knowledge.update(
            accounts=k_accounts, categories=k_categories, transactions=k_transactions
        )
        self.store.schedule_save()
        self._synced_once = True
        self._after_sync(old, new)
        return new

    def _merge(
        self,
        old: YnabData | None,
        raw_accounts: list[dict],
        raw_groups: list[dict],
        raw_txs: list[dict],
        month: dict,
    ) -> YnabData:
        accounts = dict(old.accounts) if old else {}
        for raw in raw_accounts:
            acc = Account.from_api(raw)
            if acc.deleted:
                accounts.pop(acc.id, None)
            else:
                accounts[acc.id] = acc

        categories = dict(old.categories) if old else {}
        for cat in categories_from_groups(raw_groups):
            if cat.deleted:
                categories.pop(cat.id, None)
            else:
                categories[cat.id] = cat

        transactions = dict(old.transactions) if old else {}
        for raw in raw_txs:
            tx = Transaction.from_api(raw)
            if tx.deleted:
                transactions.pop(tx.id, None)
            else:
                transactions[tx.id] = tx
        cutoff = dt_util.now().date() - timedelta(days=WINDOW_DAYS)
        transactions = {i: t for i, t in transactions.items() if t.date >= cutoff}

        return YnabData(
            accounts=accounts,
            categories=categories,
            transactions=transactions,
            ready_to_assign=to_decimal(month.get("to_be_budgeted") or 0),
            month=month["month"],
            currency=self.currency,
        )

    def _after_sync(self, old: YnabData | None, new: YnabData) -> None:
        store = self.store
        if store.initialized:
            fresh = sorted(
                (
                    t
                    for t in new.transactions.values()
                    if t.id not in store.seen_transaction_ids
                ),
                key=lambda t: (t.date, t.id),
            )
            for tx in fresh:
                # Hand-entered and scheduled transactions have no import_id.
                if tx.import_id:
                    self._fire_imported(tx)
        store.seen_transaction_ids |= set(new.transactions)
        store.prune_seen(new.transactions)
        store.initialized = True

        if old is not None and new.ready_to_assign > old.ready_to_assign:
            payload = {
                "entry_id": self.config_entry.entry_id,
                "old": float(old.ready_to_assign),
                "new": float(new.ready_to_assign),
                "delta": float(new.ready_to_assign - old.ready_to_assign),
            }
            self.hass.bus.async_fire(EVENT_READY_TO_ASSIGN_INCREASED, payload)

        store.low_state = {
            cat.id: is_low(cat, self.low_share(cat.id))
            for cat in _eligible(new.categories)
        }
        store.schedule_save()

        for cb in list(self.on_synced):
            try:
                cb(old, new)
            except Exception:  # noqa: BLE001 - one listener must not break a sync
                _LOGGER.exception("YNAB sync listener failed")

    def _fire_imported(self, tx: Transaction) -> None:
        payload = {
            "entry_id": self.config_entry.entry_id,
            "transaction_id": tx.id,
            "account_id": tx.account_id,
            "account_name": tx.account_name,
            "import_payee_name": tx.import_payee_name,
            "payee_name": tx.payee_name,
            "category_id": tx.category_id,
            "category_name": tx.category_name,
            "amount": float(tx.amount),
            "direction": direction(tx.amount),
            "date": tx.date.isoformat(),
            "day": tx.date.day,
            "approved": tx.approved,
            "cleared": tx.cleared,
            "memo": tx.memo,
        }
        self.hass.bus.async_fire(EVENT_TRANSACTION_IMPORTED, payload)


def _eligible(categories: dict[str, Category]) -> list[Category]:
    return [
        c
        for c in categories.values()
        if not c.hidden
        and not c.deleted
        and c.group_name not in EXCLUDED_GROUP_NAMES
        and c.name != RTA_CATEGORY_NAME
    ]
