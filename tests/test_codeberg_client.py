"""RED-class: B. Boundary unit — public ``CodebergClient`` contract.

Tests assert the observable contract of ``forgejo_to_github.codeberg.CodebergClient``
against simple fake HTTP transport objects. No live network access and no
credentials are required.

Coverage map (future ``CodebergClient`` behavior per test-framework spec §9):

Listing:
* Issues are listed page-by-page until exhaustion; the final empty page
  terminates iteration.
* Listing issues sends the correct request path and query parameters
  (state, type=issues, page, limit).
* Listing comments sends the correct request path with the ``issue_id``
  query parameter and follows pagination identically.

Single-resource fetch:
* Fetching a single issue by number returns the parsed payload.
* HTTP 404 translates to ``CodebergNotFoundError`` carrying the issue
  number and the URL.
* HTTP 401/403 translates to ``CodebergAuthError``.
* Transport errors (connection refused, DNS failure) translate to
  ``CodebergTransportError`` and do not leak token values.
* HTTP 429 translates to ``CodebergRateLimitError`` carrying a
  ``retry_after`` field when the header is present.

RED note: this file is intended to fail (RED) until the
``forgejo_to_github.codeberg`` module and its ``CodebergClient`` class
exist with the documented public boundary. If the import itself is the
intended RED failure, that is acceptable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from forgejo_to_github.codeberg import CodebergClient

# ---------------------------------------------------------------------------
# fake response / transport
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    """Minimal stand-in for an HTTP response.

    The client is expected to read ``status_code``, ``headers``, ``url``,
    and ``json()`` from the response object.
    """

    status_code: int
    json_payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    def json(self) -> Any:
        if self.json_payload is None:
            raise ValueError("no json body")
        return self.json_payload


@dataclass
class FakeRequest:
    """Captured record of an outbound request."""

    method: str
    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] | None = None


class FakeTransport:
    """In-memory HTTP transport with a scripted response queue.

    Responses are popped in order for each call. If a script entry is an
    ``Exception`` instance it is raised instead of returned.
    """

    def __init__(self, responses: list[FakeResponse | Exception] | None = None) -> None:
        self._scripted: list[FakeResponse | Exception] = list(responses or [])
        self.calls: list[FakeRequest] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FakeResponse:
        self.calls.append(
            FakeRequest(method=method, url=url, params=params, headers=headers)
        )
        if not self._scripted:
            raise AssertionError(
                f"FakeTransport: no scripted response for call {len(self.calls)} "
                f"({method} {url})"
            )
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def push(self, response: FakeResponse | Exception) -> None:
        self._scripted.append(response)


def _client(transport: FakeTransport) -> CodebergClient:
    """Construct a CodebergClient wired to the given fake transport."""
    return CodebergClient(
        base_url="https://codeberg.org",
        owner="acme",
        repo="widgets",
        token=None,
        transport=transport,
    )


def _token_client(
    transport: FakeTransport, token: str = "secret-token"
) -> CodebergClient:
    return CodebergClient(
        base_url="https://codeberg.org",
        owner="acme",
        repo="widgets",
        token=token,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# listing — issues
# ---------------------------------------------------------------------------


def test_list_issues_paginates_until_empty_page() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload=[{"id": 1, "number": 1}, {"id": 2, "number": 2}],
            ),
            FakeResponse(
                status_code=200,
                json_payload=[{"id": 3, "number": 3}],
            ),
            FakeResponse(status_code=200, json_payload=[]),
        ]
    )
    client = _client(transport)

    issues = list(client.list_issues())

    assert [i["number"] for i in issues] == [1, 2, 3]
    assert len(transport.calls) == 3


def test_list_issues_sends_expected_request_params() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=200, json_payload=[]),
        ]
    )
    client = _client(transport)

    list(client.list_issues())

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "GET"
    assert call.url == "https://codeberg.org/api/v1/repos/acme/widgets/issues"
    assert call.params is not None
    # Page-by-page iteration starts at page 1; caller-side state filtering
    # is not the client's responsibility per spec §9.1.
    assert call.params.get("page") == 1
    assert call.params.get("type") == "issues"
    # Limit must be a positive integer; assert the key is present.
    assert isinstance(call.params.get("limit"), int)
    assert call.params["limit"] > 0


def test_list_issues_sets_json_accept_and_user_agent() -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=[])]
    )
    client = _client(transport)

    list(client.list_issues())

    headers = transport.calls[0].headers or {}
    assert headers.get("Accept") == "application/json"
    assert headers.get("User-Agent")


def test_list_issues_omits_auth_header_when_no_token() -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=[])]
    )
    client = _client(transport)

    list(client.list_issues())

    headers = transport.calls[0].headers or {}
    assert "Authorization" not in headers


def test_list_issues_sends_token_authorization_when_configured() -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=[])]
    )
    client = _token_client(transport, token="super-secret")

    list(client.list_issues())

    headers = transport.calls[0].headers or {}
    assert headers.get("Authorization") == "token super-secret"


# ---------------------------------------------------------------------------
# listing — comments
# ---------------------------------------------------------------------------


def test_list_comments_passes_issue_id_param_and_paginates() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload=[{"id": 10}, {"id": 11}],
            ),
            FakeResponse(status_code=200, json_payload=[]),
        ]
    )
    client = _client(transport)

    comments = list(client.list_comments(issue_id=42))

    assert [c["id"] for c in comments] == [10, 11]
    assert len(transport.calls) == 2

    first = transport.calls[0]
    assert first.method == "GET"
    assert (
        first.url == "https://codeberg.org/api/v1/repos/acme/widgets/issues/42/comments"
    )
    assert first.params is not None
    assert first.params.get("issue_id") == 42
    assert first.params.get("page") == 1


# ---------------------------------------------------------------------------
# single-resource fetch
# ---------------------------------------------------------------------------


def test_get_issue_returns_parsed_payload() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload={"id": 1, "number": 7, "title": "hi"},
            )
        ]
    )
    client = _client(transport)

    issue = client.get_issue(issue_number=7)

    assert issue == {"id": 1, "number": 7, "title": "hi"}
    assert transport.calls[0].url.endswith("/issues/7")


def test_get_issue_404_raises_not_found_with_context() -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=404, json_payload={"message": "not found"})]
    )
    client = _client(transport)

    from forgejo_to_github.codeberg import CodebergNotFoundError

    with pytest.raises(CodebergNotFoundError) as excinfo:
        client.get_issue(issue_number=99)

    err = excinfo.value
    assert err.issue_number == 99
    assert err.url.endswith("/issues/99")


@pytest.mark.parametrize("status", [401, 403])
def test_get_issue_auth_errors_raise_codeberg_auth_error(status: int) -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=status, json_payload={"message": "nope"})]
    )
    client = _client(transport)

    from forgejo_to_github.codeberg import CodebergAuthError

    with pytest.raises(CodebergAuthError):
        client.get_issue(issue_number=1)


# ---------------------------------------------------------------------------
# error translation — transport + rate-limit
# ---------------------------------------------------------------------------


def test_transport_error_translates_to_codeberg_transport_error() -> None:
    class _ConnRefused(RuntimeError):
        pass

    transport = FakeTransport(responses=[_ConnRefused("connection refused")])
    client = _client(transport)

    from forgejo_to_github.codeberg import CodebergTransportError

    with pytest.raises(CodebergTransportError):
        list(client.list_issues())


def test_transport_error_does_not_leak_token() -> None:
    class _Boom(RuntimeError):
        def __str__(self) -> str:
            return "failure involving super-secret-token"

    transport = FakeTransport(responses=[_Boom()])
    client = _token_client(transport, token="super-secret-token")

    from forgejo_to_github.codeberg import CodebergTransportError

    with pytest.raises(CodebergTransportError) as excinfo:
        list(client.list_issues())

    assert "super-secret-token" not in str(excinfo.value)


def test_429_translates_to_rate_limit_error_with_retry_after() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=429,
                json_payload={"message": "rate limited"},
                headers={"Retry-After": "30"},
            )
        ]
    )
    client = _client(transport)

    from forgejo_to_github.codeberg import CodebergRateLimitError

    with pytest.raises(CodebergRateLimitError) as excinfo:
        client.get_issue(issue_number=1)

    err = excinfo.value
    assert err.retry_after == 30


def test_429_without_retry_after_header_still_raises_rate_limit_error() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=429,
                json_payload={"message": "rate limited"},
                headers={},
            )
        ]
    )
    client = _client(transport)

    from forgejo_to_github.codeberg import CodebergRateLimitError

    with pytest.raises(CodebergRateLimitError):
        client.get_issue(issue_number=1)
