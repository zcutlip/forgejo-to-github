"""RED-class: B. Boundary unit — public ``GitHubClient`` contract.

Tests assert the observable contract of ``forgejo_to_github.github.GitHubClient``
against simple fake HTTP transport objects. No live network access and no
credentials are required.

Coverage map (future ``GitHubClient`` behavior per test-framework spec §10):

Repository creation:
* ``create_repository`` POSTs to ``/user/repos`` with the expected payload
  (``name``, ``private``, ``description``, ``has_issues``) when private.
* ``create_repository`` posts ``private=False`` when public.
* The ``description`` field is included in the payload when provided.

Issue creation:
* ``create_issue`` POSTs ``{title, body, labels}`` to
  ``/repos/{owner}/{repo}/issues``.
* The response's ``number`` is parsed and returned.

Comment creation:
* ``create_comment`` POSTs ``{body}`` to the comments endpoint.
* The response's ``id`` is parsed and returned.

Issue close:
* ``close_issue`` PATCHes ``{state: "closed"}`` to the issue endpoint.

Labels:
* ``ensure_label`` POSTs ``{name, color, description}`` when missing.
* An existing label (GET returns it) is not re-posted.

Error translation:
* HTTP 422 translates to ``GitHubValidationError`` carrying parsed errors.
* HTTP 401/403 translates to ``GitHubAuthError``.
* A 403 with ``X-RateLimit-Remaining: 0`` translates to
  ``GitHubRateLimitError`` carrying the reset timestamp.
* Three consecutive secondary-rate-limit responses terminate with a
  structured ``GitHubRateLimitError`` rather than retrying forever.

RED note: this file is intended to fail (RED) until the
``forgejo_to_github.github`` module and its ``GitHubClient`` class exist
with the documented public boundary. If the import itself is the intended
RED failure, that is acceptable.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from forgejo_to_github.github import GitHubClient

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
    json_body: Any = None


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
        json_body: Any = None,
    ) -> FakeResponse:
        self.calls.append(
            FakeRequest(
                method=method,
                url=url,
                params=params,
                headers=headers,
                json_body=json_body,
            )
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


def _client(
    transport: FakeTransport,
    *,
    token: str | None = None,
    owner: str = "acme",
    repo: str = "widgets",
) -> GitHubClient:
    """Construct a GitHubClient wired to the given fake transport."""
    return GitHubClient(
        base_url="https://api.github.com",
        owner=owner,
        repo=repo,
        token=token,
        transport=transport,
    )


def _collect_calls(transport: FakeTransport) -> Iterator[FakeRequest]:
    return iter(transport.calls)


# ---------------------------------------------------------------------------
# 10.5 repository description / 10.0 repository creation
# ---------------------------------------------------------------------------


def test_create_repository_private_posts_expected_payload() -> None:
    """``create_repository`` POSTs to ``/user/repos`` with the documented
    field set when private.
    """
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=201,
                json_payload={"id": 1, "name": "widgets", "private": True},
                url="https://api.github.com/user/repos",
            )
        ]
    )
    client = _client(transport)

    result = client.create_repository(
        name="widgets",
        description="Migrated from Codeberg",
        public=False,
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.method == "POST"
    assert call.url == "https://api.github.com/user/repos"
    assert call.json_body == {
        "name": "widgets",
        "private": True,
        "description": "Migrated from Codeberg",
        "has_issues": True,
    }
    assert result["name"] == "widgets"


def test_create_repository_public_posts_private_false() -> None:
    """``create_repository`` posts ``private=False`` when public."""
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=201,
                json_payload={"id": 2, "name": "widgets", "private": False},
            )
        ]
    )
    client = _client(transport)

    client.create_repository(
        name="widgets",
        description="Open source project",
        public=True,
    )

    payload = transport.calls[0].json_body
    assert isinstance(payload, dict)
    assert payload["name"] == "widgets"
    assert payload["private"] is False
    assert payload["description"] == "Open source project"


def test_create_repository_includes_description_when_provided() -> None:
    """A non-empty description is included verbatim in the POST payload."""
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=201,
                json_payload={"id": 3, "name": "widgets"},
            )
        ]
    )
    client = _client(transport)

    client.create_repository(
        name="widgets",
        description="Copied from Codeberg",
        public=False,
    )

    payload = transport.calls[0].json_body
    assert payload["description"] == "Copied from Codeberg"


# ---------------------------------------------------------------------------
# 10.1 issue creation
# ---------------------------------------------------------------------------


def test_create_issue_posts_expected_payload_and_returns_number() -> None:
    """``create_issue`` POSTs ``{title, body, labels}`` to the issues
    endpoint and returns the parsed ``number``.
    """
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=201,
                json_payload={"number": 7, "title": "First issue"},
            )
        ]
    )
    client = _client(transport)

    number = client.create_issue(
        title="First issue",
        body="Issue body",
        labels=["bug", "help"],
    )

    call = transport.calls[0]
    assert call.method == "POST"
    assert call.url == "https://api.github.com/repos/acme/widgets/issues"
    assert call.json_body == {
        "title": "First issue",
        "body": "Issue body",
        "labels": ["bug", "help"],
    }
    assert number == 7


# ---------------------------------------------------------------------------
# 10.2 comment creation
# ---------------------------------------------------------------------------


def test_create_comment_posts_body_and_returns_id() -> None:
    """``create_comment`` POSTs ``{body}`` and returns the parsed ``id``."""
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=201,
                json_payload={"id": 999, "body": "A comment"},
            )
        ]
    )
    client = _client(transport)

    comment_id = client.create_comment(issue_number=7, body="A comment")

    call = transport.calls[0]
    assert call.method == "POST"
    assert call.url == "https://api.github.com/repos/acme/widgets/issues/7/comments"
    assert call.json_body == {"body": "A comment"}
    assert comment_id == 999


# ---------------------------------------------------------------------------
# 10.x issue close (covered via spec §10 GitHub API surface)
# ---------------------------------------------------------------------------


def test_close_issue_patches_state_closed() -> None:
    """``close_issue`` PATCHes ``{state: "closed"}`` to the issue endpoint."""
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload={"state": "closed", "number": 7},
            )
        ]
    )
    client = _client(transport)

    client.close_issue(issue_number=7)

    call = transport.calls[0]
    assert call.method == "PATCH"
    assert call.url == "https://api.github.com/repos/acme/widgets/issues/7"
    assert call.json_body == {"state": "closed"}


# ---------------------------------------------------------------------------
# 10.3 labels
# ---------------------------------------------------------------------------


def test_ensure_label_posts_payload_when_label_missing() -> None:
    """When the label does not exist, ``ensure_label`` POSTs
    ``{name, color, description}``.
    """
    transport = FakeTransport(
        responses=[
            # GET label: not present.
            FakeResponse(status_code=404, json_payload={"message": "Not Found"}),
            # POST label: created.
            FakeResponse(
                status_code=201,
                json_payload={"name": "bug", "color": "f29513"},
            ),
        ]
    )
    client = _client(transport)

    client.ensure_label(name="bug", color="f29513", description="Something is broken")

    assert len(transport.calls) == 2
    get_call, post_call = transport.calls
    assert get_call.method == "GET"
    assert post_call.method == "POST"
    assert post_call.url == "https://api.github.com/repos/acme/widgets/labels"
    assert post_call.json_body == {
        "name": "bug",
        "color": "f29513",
        "description": "Something is broken",
    }


def test_ensure_label_does_not_repost_when_label_already_exists() -> None:
    """When the GET returns the existing label, no POST is issued."""
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload={"name": "bug", "color": "f29513"},
            )
        ]
    )
    client = _client(transport)

    client.ensure_label(name="bug", color="f29513", description="Something is broken")

    assert len(transport.calls) == 1
    assert transport.calls[0].method == "GET"


# ---------------------------------------------------------------------------
# 10.1 error translation — 422 validation, 401/403 auth
# ---------------------------------------------------------------------------


def test_create_issue_422_raises_validation_error_with_messages() -> None:
    """HTTP 422 translates to ``GitHubValidationError`` carrying parsed
    error messages from the response body.
    """
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=422,
                json_payload={
                    "message": "Validation Failed",
                    "errors": [
                        {"resource": "Issue", "field": "title", "code": "missing"}
                    ],
                },
            )
        ]
    )
    client = _client(transport)

    from forgejo_to_github.github import GitHubValidationError

    with pytest.raises(GitHubValidationError) as excinfo:
        client.create_issue(title="", body="x", labels=[])

    err = excinfo.value
    assert err.messages  # populated from response body
    assert "title" in str(err.messages)


@pytest.mark.parametrize("status", [401, 403])
def test_create_issue_auth_errors_raise_github_auth_error(status: int) -> None:
    """HTTP 401/403 translates to ``GitHubAuthError``."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=status, json_payload={"message": "nope"})]
    )
    client = _client(transport)

    from forgejo_to_github.github import GitHubAuthError

    with pytest.raises(GitHubAuthError):
        client.create_issue(title="t", body="b", labels=[])


# ---------------------------------------------------------------------------
# 10.4 secondary rate limiting
# ---------------------------------------------------------------------------


def test_403_with_zero_rate_limit_remaining_raises_rate_limit_error() -> None:
    """A 403 with ``X-RateLimit-Remaining: 0`` translates to
    ``GitHubRateLimitError`` carrying the reset timestamp.
    """
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=403,
                json_payload={"message": "API rate limit exceeded"},
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1700000000",
                },
            )
        ]
    )
    client = _client(transport)

    from forgejo_to_github.github import GitHubRateLimitError

    with pytest.raises(GitHubRateLimitError) as excinfo:
        client.create_issue(title="t", body="b", labels=[])

    err = excinfo.value
    assert err.reset == 1700000000


def test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error() -> None:
    """After three consecutive secondary-rate-limit responses the client
    terminates with a structured ``GitHubRateLimitError``.
    """
    rate_limited = FakeResponse(
        status_code=429,
        json_payload={"message": "secondary rate limit"},
        headers={"Retry-After": "1"},
    )
    transport = FakeTransport(responses=[rate_limited, rate_limited, rate_limited])
    client = _client(transport)

    from forgejo_to_github.github import GitHubRateLimitError

    with pytest.raises(GitHubRateLimitError):
        client.create_issue(title="t", body="b", labels=[])

    # Three attempts before giving up; no fourth request issued.
    assert len(transport.calls) == 3
    assert all(c.method == "POST" for c in transport.calls)
