"""PayPal Transaction Search through APIMatic's generated PayPal Python SDK (`paypal`): the balance PayPal holds for
us, and the transactions it has recorded, for checking our books against PayPal's.

Built from the contract sheet in `paypal-plan.md` (APIMatic's PayPal Context Plugin workflow). One sync client for
the process, built on first use and closed at shutdown; the default retry policy stays on (both calls are GETs).
Every failure kind becomes a `ReportingError` here, so callers handle one type.

Excerpt from Hearthline: the only change is that configuration comes from environment variables
(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_ENVIRONMENT) instead of the application's settings.
"""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx2
from paypal import PaypalClient
from paypal.core import ApiError, ClientCredentials, OAuthProviderError, RawError, UnsetType
from paypal.models import DefaultError, Money, TransactionInformation
from pydantic import ValidationError

BASE_URLS = {"sandbox": "https://api-m.sandbox.paypal.com", "live": "https://api-m.paypal.com"}
PAGE_SIZE = 500  # the largest page Transaction Search returns
MAX_PAGES = 40
NO_DATA_YET = "Data for the given start date is not available"

_lock = threading.Lock()
_client: PaypalClient | None = None


class ReportingError(Exception):
    """A PayPal reporting call failed. `status_code` is what our API answers; the message is safe to show."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Balance:
    available: Decimal
    total: Decimal
    as_of: datetime | None  # PayPal refreshes balances with a delay of up to three hours


@dataclass(frozen=True)
class Transaction:
    id: str
    event_code: str  # PayPal's transaction event code, e.g. T0001 payout sent, T0006 checkout payment
    status: str  # S success, P pending, D denied, V reversed
    amount: Decimal  # signed: money in is positive
    fee: Decimal
    at: datetime | None
    reference_id: str | None  # the related transaction (a refund points at the original payment)
    custom: str | None  # for payouts, the application's own sender_item_id
    currency: str = "USD"  # a payment in another currency stays in it here; PayPal then reports a conversion


def _value[T](field: T | UnsetType) -> T | None:
    return None if isinstance(field, UnsetType) else field


def _money(field: Money | UnsetType) -> Decimal:
    money = _value(field)
    return Decimal(money.value) if money is not None else Decimal(0)


def _currency(field: Money | UnsetType) -> str:
    money = _value(field)
    return (money.currency_code if money is not None else None) or "USD"


def _time(text: str | UnsetType) -> datetime | None:
    raw = _value(text)
    if not raw:
        return None
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def client() -> PaypalClient:
    """The process's PayPal reporting client: thread-safe, long-lived, with an explicit base URL."""
    global _client
    with _lock:
        if _client is None:
            client_id, secret = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
            if not client_id or not secret:
                raise ReportingError(503, "PayPal isn't configured on this server")
            _client = PaypalClient(
                base_url=BASE_URLS[os.environ.get("PAYPAL_ENVIRONMENT", "sandbox")],
                timeout=20.0,
                oauth2=ClientCredentials(client_id=client_id, client_secret=secret),
            )
        return _client


def close() -> None:
    """Release the connection pool (app shutdown)."""
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None


def _failure(status: int, error: object) -> ReportingError:
    """One mapping for every failure, keyed on the status and the decoded error body."""
    match (status, error):
        case (401 | 403, _) | (_, OAuthProviderError()):
            return ReportingError(502, "PayPal refused our credentials for Transaction Search")
        case (429, _):
            return ReportingError(503, "PayPal is rate-limiting reporting calls; try again in a minute")
        case (s, DefaultError() as body) if 400 <= s < 500:
            return ReportingError(400, f"PayPal rejected the report request: {_value(body.message) or 'invalid'}")
        case (s, RawError()) if 400 <= s < 500:
            return ReportingError(400, "PayPal rejected the report request")
        case _:
            return ReportingError(502, "PayPal's reporting service is unavailable")


def _guard[**P, R](call: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Run a reporting call inside the one error boundary."""

    def wrap(fn: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(fn)
        def inner(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except ApiError as e:
                raise _failure(e.status_code, e.error) from e
            except ValidationError as e:
                raise ReportingError(502, f"PayPal's {call} answer couldn't be read") from e
            except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout, httpx2.ProxyError) as e:
                raise ReportingError(502, "Couldn't reach PayPal") from e
            except httpx2.RequestError as e:
                raise ReportingError(504, "PayPal didn't answer in time") from e

        return inner

    return wrap


@_guard("balance")
def balance(currency: str = "USD") -> Balance:
    response = client().transaction_search.search_balances(currency_code=currency)
    for info in _value(response.balances) or []:
        if info.currency == currency:
            total = _money(info.total_balance)
            available = _money(info.available_balance) if _value(info.available_balance) else total
            return Balance(available=available, total=total, as_of=_time(response.last_refresh_time))
    return Balance(available=Decimal(0), total=Decimal(0), as_of=_time(response.last_refresh_time))


def _transaction(info: TransactionInformation) -> Transaction | None:
    transaction_id = _value(info.transaction_id)
    if not transaction_id:
        return None
    return Transaction(
        id=transaction_id,
        event_code=_value(info.transaction_event_code) or "",
        status=_value(info.transaction_status) or "",
        amount=_money(info.transaction_amount),
        fee=_money(info.fee_amount),
        at=_time(info.transaction_initiation_date),
        reference_id=_value(info.paypal_reference_id),
        custom=_value(info.custom_field),
        currency=_currency(info.transaction_amount),
    )


@_guard("transaction search")
def transactions(start: datetime, end: datetime) -> tuple[list[Transaction], datetime | None]:
    """Balance-affecting transactions between `start` and `end` (at most 31 days apart), every page, and when
    PayPal last refreshed its report (newer activity may not be in it yet)."""
    found: list[Transaction] = []
    refreshed: datetime | None = None
    page, pages = 1, 1
    while page <= min(pages, MAX_PAGES):
        try:
            response = client().transaction_search.search_transactions(
                _rfc3339(start),
                _rfc3339(end),
                fields="transaction_info",
                balance_affecting_records_only="Y",
                page_size=PAGE_SIZE,
                page=page,
            )
        except ApiError as e:
            # PayPal says so explicitly when the window starts after its latest report: no data yet, not a failure.
            if page == 1 and e.status_code == 404 and isinstance(e.error, RawError) and NO_DATA_YET in e.error.text():
                return [], None
            raise
        refreshed = refreshed or _time(response.last_refreshed_datetime)
        pages = _value(response.total_pages) or 1
        for detail in _value(response.transaction_details) or []:
            info = _value(detail.transaction_info)
            if info is not None and (t := _transaction(info)) is not None:
                found.append(t)
        page += 1
    return found, refreshed
