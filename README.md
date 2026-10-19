# PayPal Transaction Search with APIMatic's PayPal Context Plugin

An excerpt from **Hearthline**, a project for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com). The
full repository stays private until the hackathon submission. This excerpt shows how its PayPal Transaction Search
integration was built with **[APIMatic's PayPal Context Plugin](https://github.com/apimatic/context-plugins-installer)**
on **APIMatic's generated PayPal Python SDK** (`paypal` 2.29).

In Hearthline it powers two things for the staff who run the money:

- **The PayPal balance**, checked before a batch of payouts, so a batch the account can't cover is caught before
  PayPal refuses it.
- **Books against PayPal**: the application's ledger reconciled with PayPal's own transactions. Every payout must
  trace to a decision people approved, every payment received must be in the books, and amounts must agree.

## How the plugin was used

The plugin (`npx context-plugins install paypal`, installed into Claude Code as `paypal@context-plugins` 0.4.1) gives
the coding agent skills grounded in the SDK's own source, instead of what a model remembers about PayPal:

1. **Plan before code.** Following the plugin's `python-integrate-paypal` workflow, the agent installed the SDK from
   source, read the SDK map (`sdk-map.md`, `map/operations/transaction_search.md`) and the installed modules, ran a
   read-only smoke test of both operations against the PayPal sandbox, and wrote [`paypal-plan.md`](paypal-plan.md):
   a contract sheet with exact signatures, the positional/keyword-only split, the models and members used, each
   operation's error union, the rules from the docstrings (31-day windows, 3-hour lag) and the sync-vs-async decision.
2. **Build from the sheet.** [`hearthline_paypal/reporting.py`](hearthline_paypal/reporting.py) follows the companion
   skills:
   - one long-lived sync client with an explicit base URL (the SDK silently defaults to the sandbox), closed at
     shutdown (`python-client-initialization`);
   - `UNSET` handled as `T | UnsetType`, never as `None` (`python-models`);
   - response-narrowing defaults passed explicitly (`python-calling-endpoints`);
   - the default retry policy kept for these `GET`s (`python-configuration-resilience`);
   - one error boundary for every failure kind: an API error, a refused token fetch (which raises from the operation
     call), an unreadable body, a connection that never opened, and a reply that never came (`python-error-handling`,
     `python-authentication`).
3. **Test the way the plugin recommends.** [`tests/paypal_stub.py`](tests/paypal_stub.py) replaces only the SDK's
   transport, so the real client builds, authenticates and decodes every request (`python-testing`).
   [`tests/test_reporting.py`](tests/test_reporting.py) covers the token request, paging, field mapping, each failure
   kind, and a sandbox behaviour found during the smoke test (a window newer than PayPal's latest report answers 404
   "Data for the given start date is not available", which means no data yet).
4. **Type-check against the SDK.** The SDK ships `py.typed` and is generated under `mypy --strict`, so the plugin asks
   for the same: this code passes `mypy --strict`.

## Run it

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest
uv run mypy --strict hearthline_paypal tests/paypal_stub.py
```

Against the PayPal sandbox (an app with Transaction Search enabled):

```bash
export PAYPAL_CLIENT_ID=... PAYPAL_CLIENT_SECRET=... PAYPAL_ENVIRONMENT=sandbox
uv run python -c "from hearthline_paypal import reporting; print(reporting.balance())"
```

## License

[MIT](LICENSE)
