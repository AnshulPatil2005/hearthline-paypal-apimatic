# PayPal SDK plan: balance check and reconciliation with Transaction Search

Written while following APIMatic's **PayPal Context Plugin** for Claude Code (`paypal@context-plugins` 0.4.1,
installed with `npx context-plugins install paypal`): its `python-integrate-paypal` workflow, the
`python-getting-started` lookup layer, and the companion skills listed under *Required reading*. Every SDK fact below
comes from the SDK map (`sdk-map.md`, `map/operations/transaction_search.md`) or the installed package's own modules,
not from memory.

> This is the plan from the Hearthline repository (private until the hackathon submission), with product-specific
> scope trimmed. The contract sheet is unchanged.

## Scope

1. **PayPal balance** (`transaction_search.search_balances`): what the account holds at PayPal, shown to staff and
   checked before a batch of payouts (payouts fail when the balance can't cover them).
2. **Reconciliation** (`transaction_search.search_transactions`): the application's ledger checked against PayPal's
   transactions since the books were opened (at most 31 days back). Payouts are matched by the payout item's
   `custom_field` (the application's sender_item_id), payments received by transaction id, refunds by
   `paypal_reference_id`. Read-only.

## SDK identity (from `python-getting-started`, verified against the installed package)

| Fact | Value |
| --- | --- |
| Distribution / import root | `paypal` / `paypal` (from source: `git+https://github.com/context-plugins/paypal-python-sdk.git`, locked to commit `143e57f`) |
| Version | 2.29 (`pyproject.toml` of the clone the map was read from is the same commit) |
| Client | `PaypalClient` (sync), keyword-only constructor |
| Auth | `oauth2=ClientCredentials(client_id=..., client_secret=...)` from `paypal.core`; token fetched lazily and cached on the client |
| Base URL | only `https://api-m.sandbox.paypal.com` by default: **pass `base_url` explicitly** (sandbox / `https://api-m.paypal.com` live) |

## Decisions

- **Sync client.** The host's service layer is synchronous (FastAPI `def` routes in the thread pool, SQLAlchemy
  sessions); the sync client is thread-safe. One long-lived client, built lazily on first use and closed at shutdown
  (`python-client-initialization`).
- **Timeout** 20 s per wait (default 30 s). Whole-call budget: none in sync code.
- **Retries: keep the default policy.** Both operations are `GET`, which the default retries on 408/429/5xx and
  connection failures (`max_retries` 3, backoff 1 s × 2, capped at 60 s). Tests use `retry_options=0`.
- **Response mode:** parsed calls (both operations return a body), wrapped in one error boundary.
- **Duplicate claims:** none. Both operations only read.

## Contract sheet

### `client.transaction_search.search_balances`

- `GET /v1/reporting/balances`, auth `oauth2`
- `search_balances(*, as_of_time: str | None = None, currency_code: str | None = None, request_options=None) -> BalancesResponse`
- Call: `search_balances(currency_code="USD")`
- Returns `BalancesResponse` (`paypal/models/balances_response.py`): `balances: Optional[list[BalanceInformation]]`,
  `last_refresh_time: Optional[str]`, `as_of_time: Optional[str]`, `account_id: Optional[str]`
- `BalanceInformation` (`paypal/models/balance_information.py`): `currency: str` (required),
  `total_balance: Money` (required), `available_balance: Optional[Money]`, `withheld_balance: Optional[Money]`,
  `primary: Optional[bool]`
- `Money` (`paypal/models/money.py`): `currency_code: str`, `value: str` (both required)
- Error: `ApiError` with `.error: SearchBalancesErrorBody` = `DefaultError` [400, 403, 500] | `RawError`
  (`DefaultError` in `paypal/models/default_error.py`: `name`, `message`, `debug_id`)
- Semantics (docstring): balances take up to **3 hours** to refresh; without `as_of_time` it returns the last
  refreshed balance, so the UI shows `last_refresh_time` beside the number.

### `client.transaction_search.search_transactions`

- `GET /v1/reporting/transactions`, auth `oauth2`
- `search_transactions(start_date: str, end_date: str, *, transaction_id=None, transaction_type=None,
  transaction_status=None, transaction_amount=None, transaction_currency=None, payment_instrument_type=None,
  store_id=None, terminal_id=None, fields: str | None = "transaction_info",
  balance_affecting_records_only: str | None = "Y", page_size: int | None = 100, page: int | None = 1,
  request_options=None) -> SearchResponse`
- Positional: `start_date`, `end_date` (RFC 3339 with seconds, e.g. `2026-10-19T09:00:00Z`); everything else by name.
- Call: `search_transactions(start, end, fields="transaction_info", balance_affecting_records_only="Y",
  page_size=500, page=n)` for n = 1 .. `total_pages`. Defaults are passed explicitly: `fields` defaults to
  `transaction_info` only and `balance_affecting_records_only` to `Y` (`python-calling-endpoints`: non-`None` defaults
  narrow the response).
- Returns `SearchResponse` (`paypal/models/search_response.py`): `transaction_details:
  Optional[list[TransactionDetails]]`, `total_pages: Optional[int]`, `total_items: Optional[int]`,
  `last_refreshed_datetime: Optional[str]`
- `TransactionDetails.transaction_info: Optional[TransactionInformation]`
- `TransactionInformation` (`paypal/models/transaction_information.py`), members used: `transaction_id`,
  `paypal_reference_id`, `transaction_event_code`, `transaction_initiation_date`, `transaction_amount: Optional[Money]`,
  `fee_amount: Optional[Money]`, `transaction_status`, `custom_field` (all `Optional[...]`, no wire aliases)
- Error: **Case B**, `ApiError.error` is always `RawError` (smoke test: a 59-day range answers 400
  `INVALID_REQUEST`).
- Semantics (docstring): **at most 31 days** per request; transactions take up to **3 hours** to appear; status
  `S` success, `P` pending, `D` denied, `V` reversed.
- Found while testing against the sandbox: a window that starts after PayPal's latest report answers **404
  `INVALID_REQUEST` "Data for the given start date is not available."** That is PayPal saying it has no data yet, so
  it is treated as an empty result; any other 404 stays an error.

### Shared

- `Optional[T]` is `T | UnsetType` (`paypal/core/optionality.py`), not `typing.Optional`; `UNSET` is falsy and a
  singleton. Narrow with `isinstance(value, UnsetType)`.
- Failures that are not `ApiError` (`python-error-handling`): a failed token fetch raises `ApiError` whose `.error` is
  `OAuthProviderError`; a decode failure raises `pydantic.ValidationError`; transport failures arrive as `httpx2`
  exceptions (`ConnectError`/`ConnectTimeout`/`PoolTimeout`/`ProxyError`: never sent; other `RequestError`: no reply).
  All map to one `ReportingError(status, message)`:
  401/403 or OAuth → 502 "PayPal refused our credentials" · 429 → 503 · other 4xx → 400 with our message ·
  5xx/unreadable/transport → 502/504. Reads have no side effects, so `outcome_unknown` is not needed.

## Assumptions and blockers

- Transaction Search and the Balances API are enabled on the app (smoke test passed on 2026-10-19).
- Data lags up to 3 hours, so ledger entries newer than PayPal's `last_refreshed_datetime` are "waiting for PayPal",
  not "missing".
- No blockers.

## Required reading (all loaded before coding)

- MUST load `python-getting-started` (lookup layer) and `python-integrate-paypal` (this workflow)
- MUST load `python-client-initialization`: sync client, lifetime, explicit `base_url`
- MUST load `python-authentication`: the OAuth failure path raises from the operation call
- MUST load `python-calling-endpoints`: positional vs keyword-only, defaults that narrow responses
- MUST load `python-models`: `Optional` is `T | UnsetType`
- MUST load `python-error-handling`: one boundary, distinct failure kinds
- MUST load `python-configuration-resilience`: retries on by default for `GET`
- MUST load `python-testing`: stub transport, token response first, `retry_options=0`
