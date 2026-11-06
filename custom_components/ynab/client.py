"""HTTP client for the YNAB API. HTTP only; no Home Assistant imports."""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import date
from typing import Any

import aiohttp

from .const import API_BASE, PLAN_SEGMENT

_LOGGER = logging.getLogger(__name__)

_WINDOW_SECONDS = 3600
# YNAB allows 200 requests/hour. Polls may use all of it; everything else
# (actions, extra reads) stops at REQUEST_BUDGET so polling is never starved.
REQUEST_BUDGET = 180
REQUEST_HARD_LIMIT = 200
_TIMEOUT = aiohttp.ClientTimeout(total=30)


class YnabError(Exception):
    """Base error. Messages never contain the access token."""


class YnabAuthError(YnabError):
    """HTTP 401: the token is missing, invalid or revoked."""


class YnabRateLimitError(YnabError):
    """HTTP 429: the 200 requests/hour limit was hit."""


class YnabNotFoundError(YnabError):
    """HTTP 404: the resource does not exist."""


def _error_detail(body: Any) -> str:
    """Pull YNAB's error.detail text out of an error body, if present."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("detail"), str):
            return error["detail"]
    return ""


class YnabClient:
    """Thin async wrapper over the YNAB v1 REST API."""

    def __init__(self, session: aiohttp.ClientSession, token: str) -> None:
        self._session = session
        self._token = token
        self._request_times: deque[float] = deque()

    @property
    def requests_last_hour(self) -> int:
        """Requests made in the rolling last 3600 seconds."""
        self._prune()
        return len(self._request_times)

    def _prune(self) -> None:
        cutoff = time.time() - _WINDOW_SECONDS
        while self._request_times and self._request_times[0] <= cutoff:
            self._request_times.popleft()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        poll: bool = False,
    ) -> dict[str, Any]:
        """Send a request and return the response's ``data`` object."""
        url = f"{API_BASE}{path}"
        self._prune()
        used = len(self._request_times)
        if used >= REQUEST_HARD_LIMIT or (used >= REQUEST_BUDGET and not poll):
            raise YnabRateLimitError(
                f"Local request budget reached ({used} requests in the last hour); "
                "try again later"
            )
        self._request_times.append(time.time())
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=_TIMEOUT,
            ) as response:
                status = response.status
                try:
                    body = await response.json(content_type=None)
                except (aiohttp.ClientError, ValueError):
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.warning("YNAB %s %s failed: %s", method, path, type(err).__name__)
            raise YnabError(f"Network error talking to YNAB ({type(err).__name__})") from None

        if status < 400:
            if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                raise YnabError("YNAB returned an unexpected response")
            return body["data"]

        detail = _error_detail(body)
        _LOGGER.warning("YNAB %s %s returned HTTP %s", method, path, status)
        message = f"YNAB API error {status}" + (f": {detail}" if detail else "")
        if status == 401:
            raise YnabAuthError(message)
        if status == 404:
            raise YnabNotFoundError(message)
        if status == 429:
            raise YnabRateLimitError(message)
        raise YnabError(message)

    @staticmethod
    def _plan(plan_id: str) -> str:
        return f"/{PLAN_SEGMENT}/{plan_id}"

    async def get_user(self) -> dict[str, Any]:
        return (await self._request("GET", "/user"))["user"]

    async def list_plans(self) -> list[dict[str, Any]]:
        return (await self._request("GET", f"/{PLAN_SEGMENT}"))["plans"]

    async def get_accounts(
        self, plan_id: str, since: int | None, *, poll: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        data = await self._request(
            "GET", f"{self._plan(plan_id)}/accounts", params=_delta(since), poll=poll
        )
        return data["accounts"], data["server_knowledge"]

    async def get_categories(
        self, plan_id: str, since: int | None, *, poll: bool = False
    ) -> tuple[list[dict[str, Any]], int]:
        """Return category groups, each with nested ``categories``."""
        data = await self._request(
            "GET", f"{self._plan(plan_id)}/categories", params=_delta(since), poll=poll
        )
        return data["category_groups"], data["server_knowledge"]

    async def get_transactions(
        self,
        plan_id: str,
        since: int | None,
        since_date: date | None,
        *,
        poll: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        params = _delta(since)
        if since_date is not None:
            params["since_date"] = since_date.isoformat()
        data = await self._request(
            "GET", f"{self._plan(plan_id)}/transactions", params=params, poll=poll
        )
        return data["transactions"], data["server_knowledge"]

    async def get_month(
        self, plan_id: str, month: str, *, poll: bool = False
    ) -> dict[str, Any]:
        """``month`` is ``YYYY-MM-01`` or ``current``."""
        data = await self._request(
            "GET", f"{self._plan(plan_id)}/months/{month}", poll=poll
        )
        return data["month"]

    async def update_transactions(
        self, plan_id: str, updates: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        data = await self._request(
            "PATCH",
            f"{self._plan(plan_id)}/transactions",
            json={"transactions": updates},
        )
        return data.get("transactions", [])

    async def set_category_budgeted(
        self, plan_id: str, month: str, category_id: str, budgeted_milliunits: int
    ) -> dict[str, Any]:
        data = await self._request(
            "PATCH",
            f"{self._plan(plan_id)}/months/{month}/categories/{category_id}",
            json={"category": {"budgeted": budgeted_milliunits}},
        )
        return data["category"]


def _delta(since: int | None) -> dict[str, Any]:
    return {} if since is None else {"last_knowledge_of_server": since}
