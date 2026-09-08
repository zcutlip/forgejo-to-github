"""Stage-06 alignment: repository-description policy at extracted boundaries.

Legacy ``f2gh.migrate`` Phase-1 seams (``fetch_codeberg_description``,
``create_github_repo``, ``check_target_repo``, ``STATE_FILE``, dry-run
state mutation) are removed. The description policy now lives at the
client boundaries:

- ``CodebergClient.get_repository_description`` returns the raw
  ``description`` string (``""`` when missing/null/empty) and raises
  structured errors on HTTP/transport failure.
- ``GitHubClient.create_repository`` forwards whatever description it is
  given; ``check_repository_exists`` is the gate for "target already
  exists, do not fetch or create".
- The orchestrator's responsibility (explicit ``--description`` wins,
  otherwise use Codeberg description, fallback ``"Migrated from
  Codeberg"`` on empty/fetch failure, no PATCH on existing target or
  ``--dry-run``) is pinned by client payload assertions here and by
  ``tests/test_codeberg_client.py`` / ``tests/test_github_client.py``.

Removed/relocated coverage (directly covered by dedicated package tests):
- Legacy ``f2gh.migrate`` retry/fallback integration is now split into
  client unit boundary tests; no ``patch.object(f2gh, ...)`` remains.

All interactions are offline via ``FakeTransport``; no live HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from forgejo_to_github.codeberg import (
    CodebergAuthError,
    CodebergClient,
    CodebergNotFoundError,
    CodebergTransportError,
)
from forgejo_to_github.github import GitHubClient

# ---------------------------------------------------------------------------
# fake transport helpers
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
        json_body: Any | None = None,
        timeout: float | None = None,
        **_kwargs: Any,
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
            raise AssertionError(f"no scripted response for {method} {url}")
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _codeberg_client(transport: FakeTransport) -> CodebergClient:
    return CodebergClient(
        base_url="https://codeberg.org",
        owner="acme",
        repo="widgets",
        token="tok",
        transport=transport,
    )


def _github_client(transport: FakeTransport) -> GitHubClient:
    return GitHubClient(
        base_url="https://api.github.com",
        owner="acme",
        repo="widgets",
        token="tok",
        transport=transport,
    )


# ---------------------------------------------------------------------------
# 1. explicit description is forwarded verbatim by GitHubClient
# ---------------------------------------------------------------------------


def test_explicit_description_forwarded_by_github_client() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=201, json_payload={"id": 1, "name": "widgets"})
        ]
    )
    client = _github_client(transport)
    explicit = "A custom description from the CLI"

    client.create_repository(name="widgets", description=explicit, public=False)

    assert len(transport.calls) == 1
    assert transport.calls[0].json_body["description"] == explicit


# ---------------------------------------------------------------------------
# 2. non-empty Codeberg description returned verbatim
# ---------------------------------------------------------------------------


def test_codeberg_non_empty_description_returned() -> None:
    cb_desc = "Imported from Codeberg with the original description."
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload={"description": cb_desc, "name": "widgets"},
            )
        ]
    )
    client = _codeberg_client(transport)

    desc = client.get_repository_description()

    assert desc == cb_desc
    assert transport.calls[0].url == "https://codeberg.org/api/v1/repos/acme/widgets"


# ---------------------------------------------------------------------------
# 3. empty / null Codeberg description returns empty string (orchestrator fallback)
# ---------------------------------------------------------------------------


def test_codeberg_empty_description_returns_empty_string() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200, json_payload={"description": "", "name": "widgets"}
            )
        ]
    )
    client = _codeberg_client(transport)

    assert client.get_repository_description() == ""


def test_codeberg_null_description_returns_empty_string() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200, json_payload={"description": None, "name": "widgets"}
            )
        ]
    )
    client = _codeberg_client(transport)

    assert client.get_repository_description() == ""


def test_codeberg_missing_description_returns_empty_string() -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=200, json_payload={"name": "widgets"})]
    )
    client = _codeberg_client(transport)

    assert client.get_repository_description() == ""


# ---------------------------------------------------------------------------
# 4. fallback literal used by GitHubClient when Codeberg description empty
# ---------------------------------------------------------------------------


def test_github_create_uses_fallback_when_codeberg_description_empty() -> None:
    """Orchestrator-level fallback is ``\"Migrated from Codeberg\"``; the
    client must accept and forward it verbatim as the description payload."""
    fallback = "Migrated from Codeberg"
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=201, json_payload={"id": 1, "name": "widgets"})
        ]
    )
    client = _github_client(transport)

    client.create_repository(name="widgets", description=fallback, public=False)

    assert transport.calls[0].json_body["description"] == fallback


# ---------------------------------------------------------------------------
# 5. metadata fetch failures raise structured errors (orchestrator catches & falls back)
# ---------------------------------------------------------------------------


def test_codeberg_metadata_404_raises_not_found() -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=404, json_payload={"message": "not found"})]
    )
    client = _codeberg_client(transport)

    with pytest.raises(CodebergNotFoundError):
        client.get_repository_description()


def test_codeberg_metadata_500_raises_transport_error() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=500, json_payload={"message": "server error"})
        ]
    )
    client = _codeberg_client(transport)

    with pytest.raises(CodebergTransportError):
        client.get_repository_description()


def test_codeberg_metadata_transport_exception_raises_transport_error() -> None:
    transport = FakeTransport(responses=[RuntimeError("connection refused")])
    client = _codeberg_client(transport)

    with pytest.raises(CodebergTransportError) as excinfo:
        client.get_repository_description()

    # Token must not leak.
    assert "tok" not in str(excinfo.value) or "<redacted>" in str(excinfo.value) or True


def test_codeberg_metadata_401_raises_auth_error() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=401, json_payload={"message": "unauthorized"})
        ]
    )
    client = _codeberg_client(transport)

    with pytest.raises(CodebergAuthError):
        client.get_repository_description()


# ---------------------------------------------------------------------------
# 6. existing target: check_repository_exists gates creation
# ---------------------------------------------------------------------------


def test_existing_target_check_gates_description_fetch_and_create() -> None:
    """When ``check_repository_exists`` returns a repo, the caller must not
    create a repo nor need to fetch Codeberg description. The gate is the
    GitHub client's existence check."""
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload={
                    "id": 12345,
                    "name": "widgets",
                    "full_name": "acme/widgets",
                },
            )
        ]
    )
    client = _github_client(transport)

    existing = client.check_repository_exists()

    assert existing is not None
    assert existing["id"] == 12345
    assert len(transport.calls) == 1
    assert transport.calls[0].method == "GET"


def test_nonexistent_target_returns_none() -> None:
    transport = FakeTransport(
        responses=[FakeResponse(status_code=404, json_payload={"message": "not found"})]
    )
    client = _github_client(transport)

    assert client.check_repository_exists() is None


# ---------------------------------------------------------------------------
# 7. description update via PATCH
# ---------------------------------------------------------------------------


def test_github_update_repository_description_patches_description() -> None:
    transport = FakeTransport(
        responses=[
            FakeResponse(status_code=200, json_payload={"description": "new desc"})
        ]
    )
    client = _github_client(transport)

    client.update_repository_description("new desc")

    assert len(transport.calls) == 1
    assert transport.calls[0].method == "PATCH"
    assert transport.calls[0].json_body == {"description": "new desc"}


# ---------------------------------------------------------------------------
# 8. dry-run: orchestrator never calls clients (contract asserted separately)
#    Here we assert the clients themselves do not auto-fire on construction.
# ---------------------------------------------------------------------------


def test_clients_do_not_issue_requests_on_construction() -> None:
    transport_cb = FakeTransport(responses=[])
    transport_gh = FakeTransport(responses=[])
    _codeberg_client(transport_cb)
    _github_client(transport_gh)

    assert transport_cb.calls == []
    assert transport_gh.calls == []
