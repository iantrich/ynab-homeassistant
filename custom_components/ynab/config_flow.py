"""Config and options flow for the YNAB integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp
import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client import YnabAuthError, YnabClient, YnabError
from .const import (
    DEFAULT_LOW_SHARE,
    DEFAULT_SCAN_MINUTES,
    DOMAIN,
    EXCLUDED_GROUP_NAMES,
    MAX_SCAN_MINUTES,
    MIN_SCAN_MINUTES,
    STAGES,
)

CONF_API_TOKEN = "api_token"
CONF_PLAN_ID = "plan_id"
CONF_CURRENCY = "currency"
CONF_PLAN_NAME = "plan_name"
CONF_SCAN_MINUTES = "scan_minutes"
CONF_LOW_SHARE = "low_share"
CONF_ENTITY_ACCOUNTS = "entity_accounts"
CONF_ENTITY_CATEGORIES = "entity_categories"
CONF_OVERFLOW = "overflow_category_id"
CONF_CATEGORY = "category_id"
CONF_SHARE = "share"

NO_STAGE = "none"
MIN_LOW_SHARE = 0.05
MAX_LOW_SHARE = 0.9

TOKEN_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
        )
    }
)


def _options(items: list[tuple[str, str]]) -> list[SelectOptionDict]:
    return [SelectOptionDict(value=value, label=label) for value, label in items]


async def _validate_token(hass, token: str) -> tuple[YnabClient, str | None]:
    """Return a client and an error key (None when the token works)."""
    client = YnabClient(async_get_clientsession(hass), token)
    try:
        await client.get_user()
    except YnabAuthError:
        return client, "invalid_auth"
    except (YnabError, aiohttp.ClientError, TimeoutError):
        return client, "cannot_connect"
    return client, None


class YnabConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect a token, then pick the plan (budget) to track."""

    VERSION = 1

    def __init__(self) -> None:
        self._token: str | None = None
        self._plans: dict[str, dict[str, Any]] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> YnabOptionsFlow:
        return YnabOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            client, error = await _validate_token(self.hass, user_input[CONF_API_TOKEN])
            if error is None:
                try:
                    plans = await client.list_plans()
                except YnabAuthError:
                    error = "invalid_auth"
                except (YnabError, aiohttp.ClientError, TimeoutError):
                    error = "cannot_connect"
                else:
                    if not plans:
                        return self.async_abort(reason="no_plans")
                    self._token = user_input[CONF_API_TOKEN]
                    self._plans = {p["id"]: p for p in plans}
                    return await self.async_step_plan()
            errors["base"] = error
        return self.async_show_form(
            step_id="user", data_schema=TOKEN_SCHEMA, errors=errors
        )

    async def async_step_plan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            plan_id = user_input[CONF_PLAN_ID]
            plan = self._plans[plan_id]
            await self.async_set_unique_id(plan_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=plan["name"],
                data={
                    CONF_API_TOKEN: self._token,
                    CONF_PLAN_ID: plan_id,
                    CONF_PLAN_NAME: plan["name"],
                    CONF_CURRENCY: (plan.get("currency_format") or {}).get(
                        "iso_code", "USD"
                    ),
                },
            )
        schema = probatio.Schema(
            {
                probatio.Required(CONF_PLAN_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=_options(
                            [(pid, p["name"]) for pid, p in self._plans.items()]
                        ),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="plan", data_schema=schema)

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            client, error = await _validate_token(self.hass, user_input[CONF_API_TOKEN])
            if error is None:
                try:
                    plans = await client.list_plans()
                except YnabAuthError:
                    error = "invalid_auth"
                except (YnabError, aiohttp.ClientError, TimeoutError):
                    error = "cannot_connect"
                else:
                    if all(p["id"] != entry.data[CONF_PLAN_ID] for p in plans):
                        error = "wrong_account"
            if error is None:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]},
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"plan_name": entry.title},
        )


class YnabOptionsFlow(OptionsFlow):
    """Polling, entity selection, waterfall stages and low-bucket thresholds."""

    # -- helpers ------------------------------------------------------------------

    @property
    def _runtime(self):
        entry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return None
        return entry.runtime_data

    def _eligible_categories(self) -> list:
        runtime = self._runtime
        return runtime.coordinator.eligible_categories() if runtime else []

    def _finish(self, **updates: Any) -> ConfigFlowResult:
        return self.async_create_entry(
            data={**self.config_entry.options, **updates}
        )

    # -- steps --------------------------------------------------------------------

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["general", "stages", "low_overrides"]
        )

    async def async_step_general(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        runtime = self._runtime
        if runtime is None or runtime.coordinator.data is None:
            return self.async_abort(reason="not_loaded")
        if user_input is not None:
            return self._finish(
                **{
                    CONF_SCAN_MINUTES: int(user_input[CONF_SCAN_MINUTES]),
                    CONF_LOW_SHARE: float(user_input[CONF_LOW_SHARE]),
                    CONF_ENTITY_ACCOUNTS: user_input.get(CONF_ENTITY_ACCOUNTS, []),
                    CONF_ENTITY_CATEGORIES: user_input.get(CONF_ENTITY_CATEGORIES, []),
                }
            )

        options = self.config_entry.options
        accounts = [
            (a.id, a.name)
            for a in runtime.coordinator.data.accounts.values()
            if not a.closed
        ]
        categories = [(c.id, f"{c.group_name}: {c.name}") for c in self._eligible_categories()]
        all_accounts = [value for value, _ in accounts]
        all_categories = [value for value, _ in categories]

        def _multi(items: list[tuple[str, str]]) -> SelectSelector:
            return SelectSelector(
                SelectSelectorConfig(
                    options=_options(items),
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )

        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_SCAN_MINUTES,
                    default=options.get(CONF_SCAN_MINUTES, DEFAULT_SCAN_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_MINUTES,
                        max=MAX_SCAN_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                ),
                probatio.Required(
                    CONF_LOW_SHARE,
                    default=options.get(CONF_LOW_SHARE, DEFAULT_LOW_SHARE),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_LOW_SHARE,
                        max=MAX_LOW_SHARE,
                        step=0.05,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                probatio.Optional(
                    CONF_ENTITY_ACCOUNTS,
                    default=options.get(CONF_ENTITY_ACCOUNTS, all_accounts),
                ): _multi(accounts),
                probatio.Optional(
                    CONF_ENTITY_CATEGORIES,
                    default=options.get(CONF_ENTITY_CATEGORIES, all_categories),
                ): _multi(categories),
            }
        )
        return self.async_show_form(step_id="general", data_schema=schema)

    async def async_step_stages(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        runtime = self._runtime
        if runtime is None or runtime.coordinator.data is None:
            return self.async_abort(reason="not_loaded")

        # Group id -> name, in category order, minus excluded groups.
        groups: dict[str, str] = {}
        for cat in sorted(
            runtime.coordinator.data.categories.values(), key=lambda c: c.order
        ):
            if cat.group_name not in EXCLUDED_GROUP_NAMES and not cat.deleted:
                groups.setdefault(cat.group_id, cat.group_name)

        store = runtime.store
        if user_input is not None:
            mapping = store.stage_mapping
            for group_id, name in groups.items():
                stage = user_input.get(name, NO_STAGE)
                if stage == NO_STAGE:
                    mapping["groups"].pop(group_id, None)
                else:
                    mapping["groups"][group_id] = stage
            mapping["overflow_category_id"] = user_input.get(CONF_OVERFLOW) or None
            store.schedule_save()
            # Recompute low state and sensors from the new settings.
            await runtime.coordinator.async_request_refresh()
            return self._finish()

        stage_selector = SelectSelector(
            SelectSelectorConfig(
                options=[*STAGES, NO_STAGE],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="stage",
            )
        )
        fields: dict[Any, Any] = {
            probatio.Required(
                name, default=store.stage_mapping["groups"].get(group_id, NO_STAGE)
            ): stage_selector
            for group_id, name in groups.items()
        }
        overflow = store.stage_mapping.get("overflow_category_id")
        fields[
            probatio.Optional(
                CONF_OVERFLOW,
                description={"suggested_value": overflow} if overflow else None,
            )
        ] = SelectSelector(
            SelectSelectorConfig(
                options=_options(
                    [(c.id, f"{c.group_name}: {c.name}") for c in self._eligible_categories()]
                ),
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        return self.async_show_form(
            step_id="stages", data_schema=probatio.Schema(fields)
        )

    async def async_step_low_overrides(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        runtime = self._runtime
        if runtime is None or runtime.coordinator.data is None:
            return self.async_abort(reason="not_loaded")

        store = runtime.store
        if user_input is not None:
            category_id = user_input[CONF_CATEGORY]
            share = user_input.get(CONF_SHARE)
            if share is None:
                store.low_share_overrides.pop(category_id, None)
            else:
                store.low_share_overrides[category_id] = float(share)
            store.schedule_save()
            # Recompute low state and sensors from the new settings.
            await runtime.coordinator.async_request_refresh()
            return self._finish()

        schema = probatio.Schema(
            {
                probatio.Required(CONF_CATEGORY): SelectSelector(
                    SelectSelectorConfig(
                        options=_options(
                            [
                                (c.id, f"{c.group_name}: {c.name}")
                                for c in self._eligible_categories()
                            ]
                        ),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                probatio.Optional(CONF_SHARE): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_LOW_SHARE,
                        max=MAX_LOW_SHARE,
                        step=0.05,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="low_overrides",
            data_schema=schema,
            description_placeholders={
                "current": ", ".join(
                    f"{runtime.coordinator.data.categories[cid].name}: {share:.0%}"
                    for cid, share in store.low_share_overrides.items()
                    if cid in runtime.coordinator.data.categories
                )
                or "none"
            },
        )
