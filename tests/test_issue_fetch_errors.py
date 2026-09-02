"""Stage-06 alignment: Codeberg issue-fetch 404 at the extracted boundary.

Legacy harness (``f2gh.migrate`` + ``fetch_all_codeberg_issues`` /
``check_target_repo`` mocks) is removed in stage 06. The 404-source-not-found
contract is now exercised through the extracted seams:

* ``forgejo_to_github.codeberg.CodebergClient`` with an injected
  ``Transport`` that returns HTTP 404 → raises ``CodebergNotFoundError``
  carrying the issue number / URL, without leaking the token.
* ``forgejo_to_github.migration.MigrationOrchestrator`` with a fake
  ``CodebergClient`` whose ``list_issues`` raises ``CodebergNotFoundError``
  → the orchestrator propagates the structured error so the CLI can render
  it gracefully (no raw traceback).

Preserved behavioral contract
-----------------------------
* A 404 from Codeberg issue fetching is a structured, non-traceback
  failure that names the source repository and signals "not found" / "404".
* The error does not leak the Codeberg token.
* Tests remain offline via injected transports / fakes — no live network.

Why the legacy test body was replaced
-------------------------------------
* ``f2gh.migrate`` no longer exists (replaced by
  ``MigrationOrchestrator`` + ``f2gh._build_orchestrator`` per
  ``06-cli-wiring.md`` §3). Patching ``f2gh.fetch_all_codeberg_issues``
  would not exercise any production code.
* The exit-code / capsys "SystemExit with 'owner/missing' in output"
  shape is now the CLI's ``Reporter`` responsibility; the orchestrator
  returns a ``MigrationResult`` and never raises ``SystemExit`` itself.
  The test below therefore asserts the structured exception at the
  appropriate boundary and that no traceback is produced.

No live network and no credentials are required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from forgejo_to_github.codeberg import (
    CodebergClient,
    CodebergNotFoundError,
)
from forgejo_to_github.domain import Repository
from forgejo_to_github.migration import MigrationOrchestrator

# ---------------------------------------------------------------------------
# fakes for Transport / Orchestrator collaborators
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


class FakeTransport:
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
        json_body: Any | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        self.calls.append(
            FakeRequest(method=method, url=url, params=params, headers=headers)
        )
        if not self._scripted:
            raise AssertionError(
                f"FakeTransport: no scripted response for {method} {url}"
            )
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class _FakeGit:
    clone_called = False
    push_called = False

    def run_clone(self) -> None:
        self.clone_called = True

    def run_push(self) -> None:
        self.push_called = True


class _FakeState:
    def __init__(self) -> None:
        self.events: list[tuple[int, int]] = []

    def already_migrated(self, n: int) -> bool:
        return False

    def record_issue(self, src: int, gh: int) -> None:
        self.events.append((src, gh))


class _FakeReport:
    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []

    def issue_started(self, n: int) -> None:
        self.events.append(("started", str(n)))

    def issue_succeeded(self, src: int, gh: int) -> None:
        self.events.append(("succeeded", str(src), str(gh)))

    def issue_failed(self, src: int, reason: str) -> None:
        self.events.append(("failed", str(src), reason))

    def git_phase_finished(self, status: str) -> None:
        self.events.append(("git", status))


# ---------------------------------------------------------------------------
# 1. CodebergClient boundary — 404 translates to CodebergNotFoundError
# ---------------------------------------------------------------------------


def test_codeberg_client_list_issues_404_raises_not_found_with_url() -> None:
    """``CodebergClient.list_issues`` 404 must raise ``CodebergNotFoundError``
    carrying the URL and without leaking the token.
    """
    transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=404,
                json_payload={"message": "Not Found"},
                url="https://codeberg.org/api/v1/repos/owner/missing/issues",
            )
        ]
    )
    client = CodebergClient(
        base_url="https://codeberg.org",
        owner="owner",
        repo="missing",
        token="super-secret-cb-token",
        transport=transport,
    )

    with pytest.raises(CodebergNotFoundError) as exc_info:
        client.list_issues()

    err = exc_info.value
    assert err.url  # url is populated
    assert "owner/missing" in err.url or "missing" in err.url
    # Must signal not-found, not generic transport.
    assert "404" in str(err).lower() or "not found" in str(err).lower()
    # No token leak.
    assert "super-secret-cb-token" not in str(err)
    assert "super-secret-cb-token" not in err.url
    # Structured, not a traceback — pytest.raises ensures no unhandled traceback.


def test_codeberg_client_get_issue_404_raises_not_found_with_issue_number() -> None:
    """``CodebergClient.get_issue`` 404 must raise ``CodebergNotFoundError``
    carrying the issue number and URL.
    """
    transport = FakeTransport(
        responses=[FakeResponse(status_code=404, json_payload={"message": "Not Found"})]
    )
    client = CodebergClient(
        base_url="https://codeberg.org",
        owner="owner",
        repo="missing",
        token=None,
        transport=transport,
    )

    with pytest.raises(CodebergNotFoundError) as exc_info:
        client.get_issue(issue_number=99)

    err = exc_info.value
    assert err.issue_number == 99
    assert err.url.endswith("/issues/99")
    assert "not found" in str(err).lower() or "404" in str(err).lower()


# ---------------------------------------------------------------------------
# 2. Orchestrator boundary — 404 from list_issues propagates as structured error
# ---------------------------------------------------------------------------


def test_orchestrator_propagates_codeberg_404_without_traceback() -> None:
    """When the Codeberg seam raises ``CodebergNotFoundError`` on
    ``list_issues``, the orchestrator must propagate that structured error
    (not swallow it, not turn it into a raw traceback), and no issue
    creation must occur.
    """
    not_found = CodebergNotFoundError(
        "repository owner/missing not found on Codeberg",
        url="https://codeberg.org/api/v1/repos/owner/missing/issues",
    )

    class _FakeCodeberg404:
        def __init__(self, exc: Exception) -> None:
            self.exc = exc
            self.calls: list[str] = []

        def list_issues(self) -> list[dict[str, Any]]:
            self.calls.append("list_issues")
            raise self.exc

    class _FakeGitHub:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append("create_issue")
            return {"number": 1}

        def create_comment(
            self, issue_number: int, payload: dict[str, Any]
        ) -> dict[str, Any]:
            self.calls.append("create_comment")
            return {"id": 1}

    fake_codeberg = _FakeCodeberg404(not_found)
    fake_github = _FakeGitHub()
    fake_git = _FakeGit()
    fake_state = _FakeState()
    fake_report = _FakeReport()
    repo = Repository(source="owner/missing", target="owner/target", skip_git=True)

    orch = MigrationOrchestrator(
        repo=repo,
        codeberg=fake_codeberg,
        github=fake_github,
        git=fake_git,
        state=fake_state,
        reporter=fake_report,
    )

    with pytest.raises(CodebergNotFoundError) as exc_info:
        orch.run()

    err = exc_info.value
    # Must name the source repo.
    assert "owner/missing" in str(err)
    # Must signal not-found / 404 / source.
    lower = str(err).lower()
    assert any(sig in lower for sig in ("not found", "404", "source"))
    # No traceback string in the rendered message (structured error).
    assert "Traceback" not in str(err)
    # No issue creation must have occurred — clone was skipped, issue fetch is terminal at read time.
    assert fake_github.calls == [], (
        f"issue creation must not occur after 404, got {fake_github.calls!r}"
    )
    assert fake_state.events == []


def test_codeberg_404_error_does_not_leak_token_via_orchestrator() -> None:
    """Even when the underlying transport error string contains the token,
    the surfaced ``CodebergNotFoundError`` / ``CodebergTransportError`` must
    not leak it — redaction is enforced at the CodebergClient boundary.
    """
    # Simulate a transport-level error that somehow includes the token in its message.
    token = "cb-super-secret"

    class _Boom(RuntimeError):
        def __str__(self) -> str:
            return f"failure involving {token}"

    transport = FakeTransport(responses=[_Boom()])
    client = CodebergClient(
        base_url="https://codeberg.org",
        owner="acme",
        repo="widgets",
        token=token,
        transport=transport,
    )

    from forgejo_to_github.codeberg import CodebergTransportError

    with pytest.raises(CodebergTransportError) as exc_info:
        client.list_issues()

    assert token not in str(exc_info.value)
