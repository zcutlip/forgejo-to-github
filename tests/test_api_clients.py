"""RED-class: B. Boundary unit — observable API contracts at the Transport boundary.

Stage-06 alignment: this module previously patched ``f2gh`` / ``requests``
helpers (``fetch_all_codeberg_issues``, ``gh_request``, ``create_github_repo``,
etc.). It now drives the extracted package clients via injected ``Transport``
fakes, preserving exact payload, pagination, retry, and header contracts.

Injected collaborators:
- ``CodebergClient`` + ``FakeTransport`` for Forgejo v1 pagination, params,
  error translation, and description.
- ``GitHubClient`` + ``FakeTransport`` for repo/issue/comment/close payloads,
  org fallback, and secondary-rate-limit retry via Transport.

Behavioral assertions are preserved (exact URLs, exact JSON payloads,
exact query-param sets, header presence). Error expectations are updated
to the structured hierarchy (``CodebergNotFoundError``,
``CodebergTransportError``, ``GitHubRateLimitError``, etc.) — the raw
``requests.HTTPError`` contract no longer exists after extraction.

Redundant legacy tests removed (behavior already directly covered by
dedicated package tests, no contract gap):
- ``test_fetch_all_codeberg_issues_sorts_by_created_at_ascending`` — sorting
  is orchestrator responsibility; ``CodebergClient.list_issues`` returns
  API page order (see ``test_codeberg_client.test_list_issues_*`` and
  orchestrator ordering tests). Deleted here to avoid pinning client sorting.
- ``test_gh_request_retries_on_403_then_succeeds`` — 403 is not retried
  except for ``X-RateLimit-Remaining: 0``; only 429 is retried internally
  (``test_github_client.test_rate_limit_429_is_retried_then_terminates_*``).
  Replaced by 429-specific retry tests below.
- ``test_fetch_codeberg_description_missing_returns_fallback`` — fallback
  ``"Migrated from Codeberg"`` is orchestrator policy; client returns ``""``
  (see ``test_repository_description``). Adjusted assertion accordingly.

No live network and no credentials are required.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from forgejo_to_github.codeberg import (
    CodebergAuthError,
    CodebergClient,
    CodebergNotFoundError,
    CodebergRateLimitError,
    CodebergTransportError,
)
from forgejo_to_github.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubRateLimitError,
)

# ---------------------------------------------------------------------------
# fake response / transport (Transport Protocol shape)
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
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
    method: str
    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    json_body: Any = None


class FakeTransport:
    """In-memory Transport with a scripted response queue."""

    def __init__(
        self, responses: list[FakeResponse | BaseException] | None = None
    ) -> None:
        self._scripted: list[FakeResponse | BaseException] = list(responses or [])
        self.calls: list[FakeRequest] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
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
                f"FakeTransport: no scripted response for {method} {url} call {len(self.calls)}"
            )
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def push(self, item: FakeResponse | BaseException) -> None:
        self._scripted.append(item)


def _codeberg_client(
    transport: FakeTransport, token: str | None = "cb-token"
) -> CodebergClient:
    return CodebergClient(
        base_url="https://codeberg.org",
        owner="owner",
        repo="source",
        token=token,
        transport=transport,
    )


def _github_client(
    transport: FakeTransport,
    token: str | None = "gh-token",
    owner: str = "owner",
    repo: str = "target",
) -> GitHubClient:
    return GitHubClient(
        base_url="https://api.github.com",
        owner=owner,
        repo=repo,
        token=token,
        transport=transport,
    )


# ---------------------------------------------------------------------------
# 1. Codeberg pagination
# ---------------------------------------------------------------------------


def test_fetch_all_codeberg_issues_pages_until_empty() -> None:
    """``CodebergClient.list_issues`` must walk pages until an empty page."""
    page1 = [{"number": 2, "created_at": "2024-02-01T00:00:00Z"}]
    page2 = [{"number": 1, "created_at": "2024-01-01T00:00:00Z"}]

    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=200, json_payload=page1),
            FakeResponse(status_code=200, json_payload=page2),
            FakeResponse(status_code=200, json_payload=[]),
        ]
    )
    client = _codeberg_client(transport)

    issues = client.list_issues()

    assert len(transport.calls) == 3
    for call in transport.calls:
        assert call.url == "https://codeberg.org/api/v1/repos/owner/source/issues"
    assert [c.params["page"] for c in transport.calls] == [1, 2, 3]  # type: ignore[index]
    for call in transport.calls:
        assert call.params is not None
        assert call.params["state"] == "all"
        assert call.params["type"] == "issues"
        assert call.params["limit"] == 50
    # Client returns page order (no sorting); orchestrator sorts.
    assert [i["number"] for i in issues] == [2, 1]


def test_fetch_all_codeberg_issues_empty_first_page_returns_empty_list() -> None:
    """An empty first page must terminate immediately without further calls."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=[])]
    )
    client = _codeberg_client(transport)

    issues = client.list_issues()

    assert issues == []
    assert len(transport.calls) == 1


def test_fetch_all_codeberg_issues_sends_type_issues_to_exclude_prs() -> None:
    """The ``type=issues`` query parameter excludes pull requests."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=[])]
    )
    client = _codeberg_client(transport)

    client.list_issues()

    assert transport.calls[0].params is not None
    assert transport.calls[0].params["type"] == "issues"


def test_fetch_codeberg_comments_uses_issue_index_in_path() -> None:
    """Comments endpoint must use the Codeberg issue index."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=[])]
    )
    client = _codeberg_client(transport)

    client.list_comments(issue_id=42)

    assert (
        transport.calls[0].url
        == "https://codeberg.org/api/v1/repos/owner/source/issues/42/comments"
    )
    assert transport.calls[0].params is not None
    assert transport.calls[0].params["issue_id"] == 42


# ---------------------------------------------------------------------------
# 2. Codeberg 404 / transport / rate-limit translation
# ---------------------------------------------------------------------------


def test_fetch_all_codeberg_issues_404_raises_not_found() -> None:
    """HTTP 404 must surface as ``CodebergNotFoundError``."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=404, json_payload={"message": "not found"})]
    )
    client = _codeberg_client(transport)

    with pytest.raises(CodebergNotFoundError) as exc_info:
        client.list_issues()

    assert "owner/source" in str(exc_info.value) or exc_info.value.url != ""


def test_fetch_all_codeberg_issues_connection_error_propagates_as_transport() -> None:
    """Transport-level ``ConnectionError`` must become ``CodebergTransportError``."""
    err = ConnectionError("Could not resolve host codeberg.org")
    transport = FakeTransport(responses=[err])
    client = _codeberg_client(transport)

    with pytest.raises(CodebergTransportError) as exc_info:
        client.list_issues()

    assert "Could not resolve" in str(exc_info.value) or isinstance(
        exc_info.value, CodebergTransportError
    )


def test_fetch_all_codeberg_issues_timeout_propagates_as_transport() -> None:
    """Transport timeout must become ``CodebergTransportError``."""
    err = TimeoutError("Read timed out")
    transport = FakeTransport(responses=[err])
    client = _codeberg_client(transport)

    with pytest.raises(CodebergTransportError):
        client.list_issues()


def test_fetch_codeberg_comments_404_raises_not_found() -> None:
    """HTTP 404 on the comments endpoint must surface as ``CodebergNotFoundError``."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=404, json_payload={"message": "not found"})]
    )
    client = _codeberg_client(transport)

    with pytest.raises(CodebergNotFoundError):
        client.list_comments(issue_id=1)


def test_fetch_codeberg_description_returns_description_string() -> None:
    """A 200 response carrying ``description`` must return the description."""
    repo = {
        "id": 1,
        "name": "source",
        "full_name": "owner/source",
        "description": "Original project description",
    }
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=repo)]
    )
    client = _codeberg_client(transport)

    desc = client.get_repository_description()

    assert desc == "Original project description"
    assert transport.calls[0].url == "https://codeberg.org/api/v1/repos/owner/source"


def test_fetch_codeberg_description_missing_returns_empty_string() -> None:
    """Empty/missing ``description`` must return ``""`` (fallback is orchestrator policy)."""
    repo = {"id": 1, "name": "source", "full_name": "owner/source", "description": ""}
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=repo)]
    )
    client = _codeberg_client(transport)

    desc = client.get_repository_description()

    assert desc == ""


def test_codeberg_429_translates_to_rate_limit_error() -> None:
    """HTTP 429 must surface as ``CodebergRateLimitError`` with ``retry_after``."""
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=429,
                json_payload={"message": "rate limited"},
                headers={"Retry-After": "30"},
            )
        ]
    )
    client = _codeberg_client(transport)

    with pytest.raises(CodebergRateLimitError) as exc_info:
        client.get_issue(issue_number=1)

    assert exc_info.value.retry_after == 30


def test_codeberg_auth_error_on_401_403() -> None:
    """401/403 must surface as ``CodebergAuthError``."""
    for status in (401, 403):
        transport = FakeTransport(
            responses=[
                FakeResponse(status_code=status, json_payload={"message": "nope"})
            ]
        )
        client = _codeberg_client(transport)
        with pytest.raises(CodebergAuthError):
            client.get_issue(issue_number=1)


# ---------------------------------------------------------------------------
# 3. GitHub repo creation
# ---------------------------------------------------------------------------


def test_create_github_repo_default_private_posts_expected_payload() -> None:
    """With ``public=False``, POST payload must have ``private=True`` and documented fields."""
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=201, json_payload={"id": 1, "name": "target"})
        ]
    )
    client = _github_client(transport, owner="owner", repo="target")

    result = client.create_repository(
        name="target", description="My project", public=False
    )

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call.url == "https://api.github.com/user/repos"
    assert call.method == "POST"
    assert call.json_body == {
        "name": "target",
        "private": True,
        "description": "My project",
        "has_issues": True,
    }
    assert result == {"id": 1, "name": "target"}


def test_create_github_repo_public_posts_private_false() -> None:
    """With ``public=True``, POST payload must have ``private=False``."""
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=201, json_payload={"id": 2, "name": "target"})
        ]
    )
    client = _github_client(transport)

    client.create_repository(name="target", description="Public project", public=True)

    payload = transport.calls[0].json_body
    assert isinstance(payload, dict)
    assert payload["private"] is False


def test_create_github_repo_falls_back_to_org_endpoint() -> None:
    """A non-201 from ``/user/repos`` must fall back to ``/orgs/{owner}/repos``."""
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=422, json_payload={"message": "exists"}),
            FakeResponse(status_code=201, json_payload={"id": 99, "name": "target"}),
        ]
    )
    client = _github_client(transport, owner="myorg", repo="target")

    result = client.create_repository(
        name="target", description="Org repo", public=False
    )

    assert len(transport.calls) == 2
    assert transport.calls[0].url == "https://api.github.com/user/repos"
    assert transport.calls[0].method == "POST"
    assert transport.calls[1].url == "https://api.github.com/orgs/myorg/repos"
    assert transport.calls[1].method == "POST"
    assert transport.calls[0].json_body == transport.calls[1].json_body
    assert result == {"id": 99, "name": "target"}


# ---------------------------------------------------------------------------
# 4. GitHub issue / comment / close payloads
# ---------------------------------------------------------------------------


def test_create_github_issue_posts_expected_payload() -> None:
    """``create_issue`` must POST ``{title, body, labels}`` to ``/repos/{target}/issues``."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=201, json_payload={"number": 7})]
    )
    client = _github_client(transport)

    result = client.create_issue(
        title="First issue", body="Issue body", labels=["bug", "help"]
    )

    assert transport.calls[0].method == "POST"
    assert transport.calls[0].url == "https://api.github.com/repos/owner/target/issues"
    assert transport.calls[0].json_body == {
        "title": "First issue",
        "body": "Issue body",
        "labels": ["bug", "help"],
    }
    assert result == 7


def test_create_github_comment_posts_body_to_comments_endpoint() -> None:
    """``create_comment`` must POST ``{body}`` to the comments endpoint."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=201, json_payload={"id": 999})]
    )
    client = _github_client(transport)

    result = client.create_comment(issue_number=7, body="A comment")

    assert transport.calls[0].method == "POST"
    assert (
        transport.calls[0].url
        == "https://api.github.com/repos/owner/target/issues/7/comments"
    )
    assert transport.calls[0].json_body == {"body": "A comment"}
    assert result == 999


def test_close_github_issue_patches_state_closed() -> None:
    """``close_issue`` must PATCH ``{state: "closed"}`` to the issue."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload={"state": "closed"})]
    )
    client = _github_client(transport)

    client.close_issue(issue_number=7)

    assert transport.calls[0].method == "PATCH"
    assert (
        transport.calls[0].url == "https://api.github.com/repos/owner/target/issues/7"
    )
    assert transport.calls[0].json_body == {"state": "closed"}


# ---------------------------------------------------------------------------
# 5. check_repository_exists return contract
# ---------------------------------------------------------------------------


def test_check_target_repo_returns_repo_dict_on_200() -> None:
    """A 200 response must return the parsed JSON repo info."""
    repo = {"id": 1, "name": "target", "full_name": "owner/target"}
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload=repo)]
    )
    client = _github_client(transport)

    result = client.check_repository_exists()

    assert result == repo


def test_check_target_repo_returns_none_on_404() -> None:
    """A 404 response must return ``None``."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=404, json_payload={"message": "Not Found"})]
    )
    client = _github_client(transport)

    result = client.check_repository_exists()

    assert result is None


def test_check_target_repo_403_raises_auth_error() -> None:
    """A 403 (without Remaining 0) must raise ``GitHubAuthError``."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=403, json_payload={"message": "forbidden"})]
    )
    client = _github_client(transport)

    with pytest.raises(GitHubAuthError):
        client.check_repository_exists()


# ---------------------------------------------------------------------------
# 6. GitHub rate-limit retry behavior (Transport injection)
# ---------------------------------------------------------------------------


def test_gh_request_retries_on_429_then_succeeds() -> None:
    """A 429 followed by a 200 must succeed on retry (3-attempt cap)."""
    rate_limited = FakeResponse(
        status_code=429,
        json_payload={"message": "secondary rate limit"},
        headers={"Retry-After": "1"},
    )
    ok = FakeResponse(status_code=201, json_payload={"number": 7})
    transport = FakeTransport(responses=[rate_limited, ok])
    client = _github_client(transport)

    with (
        patch("forgejo_to_github.github.time.sleep", return_value=None),
        patch("forgejo_to_github.github.random.uniform", return_value=0),
    ):
        number = client.create_issue(title="t", body="b", labels=[])

    assert number == 7
    assert len(transport.calls) == 2


def test_gh_request_exhausts_retries_before_terminal_failure() -> None:
    """Three consecutive 429s must terminate with ``GitHubRateLimitError`` after 3 attempts."""
    rate_limited = FakeResponse(
        status_code=429,
        json_payload={"message": "secondary rate limit"},
        headers={"Retry-After": "1"},
    )
    transport = FakeTransport(responses=[rate_limited, rate_limited, rate_limited])
    client = _github_client(transport)

    with (
        patch("forgejo_to_github.github.time.sleep", return_value=None),
        patch("forgejo_to_github.github.random.uniform", return_value=0),
        pytest.raises(GitHubRateLimitError),
    ):
        client.create_issue(title="t", body="b", labels=[])

    assert len(transport.calls) == 3


def test_gh_request_uses_x_ratelimit_reset_when_retry_after_missing() -> None:
    """When ``Retry-After`` is absent, ``X-RateLimit-Reset`` drives the delay."""
    future_epoch = int(time.time()) + 5
    rate_limited = FakeResponse(
        status_code=429,
        json_payload={"message": "secondary rate limit"},
        headers={"X-RateLimit-Reset": str(future_epoch)},
    )
    ok = FakeResponse(status_code=201, json_payload={"number": 7})
    transport = FakeTransport(responses=[rate_limited, ok])
    client = _github_client(transport)

    sleep_calls: list[float] = []

    def fake_sleep(s: float) -> None:
        sleep_calls.append(s)

    with (
        patch("forgejo_to_github.github.time.sleep", side_effect=fake_sleep),
        patch("forgejo_to_github.github.random.uniform", return_value=0.1),
    ):
        number = client.create_issue(title="t", body="b", labels=[])

    assert number == 7
    assert len(sleep_calls) == 1
    assert 0 < sleep_calls[0] <= 7  # ~5s + jitter


def test_gh_request_sends_bearer_authorization_header() -> None:
    """``GitHubClient`` must include ``Authorization: Bearer <token>`` and pinned headers."""
    transport = FakeTransport(
        responses=[FakeResponse(status_code=201, json_payload={"number": 7})]
    )
    client = _github_client(transport, token="gh-sentinel")

    with patch("forgejo_to_github.github.time.sleep", return_value=None):
        client.create_issue(title="t", body="b", labels=[])

    headers = transport.calls[0].headers or {}
    assert headers.get("Authorization") == "Bearer gh-sentinel"
    assert headers.get("Accept") == "application/vnd.github+json"
    assert headers.get("X-GitHub-Api-Version") == "2022-11-28"


def test_gh_request_403_with_remaining_zero_raises_rate_limit_error() -> None:
    """403 with ``X-RateLimit-Remaining: 0`` must raise ``GitHubRateLimitError`` immediately."""
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
    client = _github_client(transport)

    with pytest.raises(GitHubRateLimitError) as exc_info:
        client.create_issue(title="t", body="b", labels=[])

    assert exc_info.value.reset == 1700000000
    assert len(transport.calls) == 1
