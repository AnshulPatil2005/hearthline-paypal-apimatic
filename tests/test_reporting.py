"""The reporting module against APIMatic's PayPal SDK, with only the network replaced (see paypal_stub.py)."""

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest

from hearthline_paypal import reporting
from tests.paypal_stub import (
    BALANCES_PATH,
    TOKEN_PATH,
    TRANSACTIONS_PATH,
    RoutingTransport,
    balances_body,
    json_response,
    stub_client,
    token_response,
    transaction,
    transactions_body,
)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> RoutingTransport:
    stub = RoutingTransport()
    monkeypatch.setattr(reporting, "_client", stub_client(stub))
    return stub


def test_balance_fetches_the_token_then_the_balance(transport: RoutingTransport) -> None:
    transport.routes[BALANCES_PATH] = json_response(200, balances_body("9247.26", refreshed="2026-10-19T09:29:59Z"))
    bal = reporting.balance()
    assert (bal.available, bal.total) == (Decimal("9247.26"), Decimal("9247.26"))
    assert bal.as_of == datetime(2026, 10, 19, 9, 29, 59, tzinfo=UTC)
    paths = [urlparse(r.url).path for r in transport.requests]
    assert paths == [TOKEN_PATH, BALANCES_PATH]
    assert transport.calls(BALANCES_PATH) == [{"currency_code": ["USD"]}]
    assert {k.lower(): v for k, v in transport.requests[-1].headers.items()}["authorization"] == "Bearer t"


def test_each_failure_kind_becomes_one_clear_error(transport: RoutingTransport) -> None:
    refused = {"name": "NOT_AUTHORIZED", "message": "Authorization failed", "debug_id": "x"}
    transport.routes[BALANCES_PATH] = json_response(403, refused)
    with pytest.raises(reporting.ReportingError) as e:
        reporting.balance()
    assert (e.value.status_code, str(e.value)) == (502, "PayPal refused our credentials for Transaction Search")

    def unsent(request: object) -> object:
        raise httpx2.ConnectError("refused")

    def no_reply(request: object) -> object:
        raise httpx2.ReadTimeout("no reply")

    transport.routes[BALANCES_PATH] = unsent  # type: ignore[assignment]
    with pytest.raises(reporting.ReportingError) as never_sent:
        reporting.balance()
    transport.routes[BALANCES_PATH] = no_reply  # type: ignore[assignment]
    with pytest.raises(reporting.ReportingError) as unknown:
        reporting.balance()
    assert (never_sent.value.status_code, unknown.value.status_code) == (502, 504)

    transport.routes[BALANCES_PATH] = json_response(200, {"balances": [{"currency": "USD"}]})  # no total_balance
    with pytest.raises(reporting.ReportingError) as unreadable:
        reporting.balance()
    assert unreadable.value.status_code == 502  # an unreadable answer is never "zero"


def test_a_refused_token_is_a_credentials_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = RoutingTransport()
    stub.routes[TOKEN_PATH] = json_response(
        401, {"error": "invalid_client", "error_description": "Client Authentication failed"}
    )
    monkeypatch.setattr(reporting, "_client", stub_client(stub))
    with pytest.raises(reporting.ReportingError) as e:
        reporting.balance()
    assert e.value.status_code == 502
    assert [urlparse(r.url).path for r in stub.requests] == [TOKEN_PATH]  # the operation was never sent
    stub.routes[TOKEN_PATH] = token_response()


def test_every_page_is_read_and_fields_are_mapped(transport: RoutingTransport) -> None:
    pages = {
        "1": transactions_body(
            [transaction("PAYOUT00000000001", "T0001", "-300.00", "2026-10-19T08:00:00Z", custom="case-5")],
            refreshed="2026-10-19T09:00:00Z",
            pages=2,
        ),
        "2": transactions_body(
            [transaction("REFUND00000000001", "T1107", "-25.00", "2026-10-19T08:05:00Z", ref="CAPTURE0000000001")],
            refreshed="2026-10-19T09:00:00Z",
            page=2,
            pages=2,
        ),
    }

    def paged(request: object) -> object:
        return json_response(200, pages[parse_qs(urlparse(request.url).query)["page"][0]])  # type: ignore[attr-defined]

    transport.routes[TRANSACTIONS_PATH] = paged  # type: ignore[assignment]
    found, refreshed = reporting.transactions(datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 19, tzinfo=UTC))
    assert [(t.id, t.event_code, t.amount, t.custom, t.reference_id) for t in found] == [
        ("PAYOUT00000000001", "T0001", Decimal("-300.00"), "case-5", None),
        ("REFUND00000000001", "T1107", Decimal("-25.00"), None, "CAPTURE0000000001"),
    ]
    assert refreshed == datetime(2026, 10, 19, 9, tzinfo=UTC)
    queries = transport.calls(TRANSACTIONS_PATH)
    assert [q["page"] for q in queries] == [["1"], ["2"]]
    assert queries[0]["start_date"] == ["2026-10-01T00:00:00Z"] and queries[0]["fields"] == ["transaction_info"]


def test_a_window_newer_than_paypals_report_means_no_data_yet(transport: RoutingTransport) -> None:
    not_yet = {"name": "INVALID_REQUEST", "message": "Data for the given start date is not available.", "debug_id": "x"}
    transport.routes[TRANSACTIONS_PATH] = json_response(404, not_yet)
    assert reporting.transactions(datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 19, 1, tzinfo=UTC)) == (
        [],
        None,
    )

    transport.routes[TRANSACTIONS_PATH] = json_response(404, {"name": "RESOURCE_NOT_FOUND", "message": "x"})
    with pytest.raises(reporting.ReportingError) as e:
        reporting.transactions(datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 19, 1, tzinfo=UTC))
    assert e.value.status_code == 400  # any other 404 is still an error
