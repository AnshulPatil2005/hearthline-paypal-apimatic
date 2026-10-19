"""A stub transport for APIMatic's PayPal SDK (`paypal`), per the plugin's python-testing skill: the real client and
its request pipeline run, only the network is replaced. Responses are chosen by URL path; the token request comes
first and is answered automatically."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from paypal import PaypalClient
from paypal.core import HttpRequest, HttpResponse

TOKEN_PATH = "/v1/oauth2/token"
BALANCES_PATH = "/v1/reporting/balances"
TRANSACTIONS_PATH = "/v1/reporting/transactions"


@dataclass
class StubResponse:
    """A canned head, with the body held as chunks until the SDK reads it."""

    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: list[bytes] = field(default_factory=list)
    url: str = "https://api-m.sandbox.paypal.com/"
    closed: bool = False

    def iter_bytes(self, chunk_size: int | None) -> Iterator[bytes]:
        yield from self.chunks
        self.close()

    def read(self) -> bytes:
        self.close()
        return b"".join(self.chunks)

    def close(self) -> None:
        self.closed = True


def json_response(status: int, body: object) -> StubResponse:
    return StubResponse(
        status_code=status, headers={"content-type": "application/json"}, chunks=[json.dumps(body).encode()]
    )


def token_response() -> StubResponse:
    return json_response(200, {"access_token": "t", "token_type": "Bearer", "expires_in": 3600})


# A route answers with a response, or raises (a transport failure).
Route = StubResponse | Callable[[HttpRequest], StubResponse]


class RoutingTransport:
    """Satisfies the SDK's sync transport protocol (send + close). Routes are keyed by URL path."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {TOKEN_PATH: token_response()}
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = urlparse(request.url).path
        route = self.routes.get(path)
        if route is None:
            return json_response(404, {"name": "RESOURCE_NOT_FOUND", "message": path, "debug_id": "stub"})
        if isinstance(route, StubResponse):
            fresh = StubResponse(route.status_code, route.headers, list(route.chunks), request.url)
            return fresh
        return route(request)

    def close(self) -> None: ...

    def calls(self, path: str) -> list[dict[str, list[str]]]:
        """The query of every request made to `path`, in order."""
        return [parse_qs(urlparse(r.url).query) for r in self.requests if urlparse(r.url).path == path]


def stub_client(transport: RoutingTransport) -> PaypalClient:
    # retry_options=0: retrying is on by default, so a stubbed 5xx on a GET would otherwise be sent four times.
    return PaypalClient(
        base_url="https://api-m.sandbox.paypal.com",
        custom_http_client=transport,
        retry_options=0,
        oauth2={"client_id": "id", "client_secret": "secret"},
    )


def balances_body(
    available: str, total: str | None = None, refreshed: str = "2026-10-19T09:00:00Z"
) -> dict[str, object]:
    return {
        "balances": [
            {
                "currency": "USD",
                "primary": True,
                "total_balance": {"currency_code": "USD", "value": total or available},
                "available_balance": {"currency_code": "USD", "value": available},
            }
        ],
        "last_refresh_time": refreshed,
    }


def transaction(
    tid: str, code: str, amount: str, at: str, *, custom: str | None = None, ref: str | None = None
) -> dict[str, object]:
    info: dict[str, object] = {
        "transaction_id": tid,
        "transaction_event_code": code,
        "transaction_initiation_date": at,
        "transaction_amount": {"currency_code": "USD", "value": amount},
        "transaction_status": "S",
    }
    if custom:
        info["custom_field"] = custom
    if ref:
        info["paypal_reference_id"] = ref
    return {"transaction_info": info}


def transactions_body(
    details: list[dict[str, object]], *, refreshed: str, page: int = 1, pages: int = 1
) -> dict[str, object]:
    return {
        "transaction_details": details,
        "last_refreshed_datetime": refreshed,
        "page": page,
        "total_pages": pages,
        "total_items": len(details),
    }
