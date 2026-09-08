"""Slice G — end-to-end parity test (locked contract, audit-remediation.md §4.6).

Drives the REAL ``MigrationOrchestrator`` through every audit-remediation
slice (A–F) simultaneously in one scenario, using REAL collaborator
classes (``CodebergClient``, ``GitHubClient``, ``GitMirror``,
``StateStore``, ``Reporter``) with only external I/O mocked (HTTP
transport, git command runner / tempdir / cleanup, output sinks).

Contract note (§4.6 vs real code): §4.6 says "the repo is created, then
the description is set from the source (or from the fallback)". Per the
Slice A decision (audit-remediation.md ledger row 6, refactor
``04-orchestrator.md`` §3.8), the orchestrator folds the description
into the ``create_repository(name, description, public)`` payload and
NEVER calls ``update_repository_description``. This test therefore
asserts the real contract: the create POST carries the fetched source
description, and no PATCH is issued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import forgejo_to_github.migration as migration_mod
from forgejo_to_github.codeberg import CodebergClient
from forgejo_to_github.domain import Repository
from forgejo_to_github.formatting import format_comment_body, format_issue_body
from forgejo_to_github.git import GitMirror
from forgejo_to_github.github import GitHubClient
from forgejo_to_github.migration import MigrationOrchestrator
from forgejo_to_github.reporting import Reporter
from forgejo_to_github.state import StateStore

# ---------------------------------------------------------------------------
# Minimal fake HTTP transport/response (mirrors test_concrete_integration.py)
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
    """Recording transport with scripted queue; supports params/headers/json_body."""

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
                f"FakeTransport: no scripted response for {method} {url} (call #{len(self.calls)})"
            )
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class EventTransport:
    """Transport decorator that logs a shared cross-seam event order."""

    def __init__(self, inner: FakeTransport, events: list[str], tag: str) -> None:
        self._inner = inner
        self._events = events
        self._tag = tag

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
        self._events.append(f"{self._tag} {method} {url}")
        return self._inner(
            method,
            url,
            params=params,
            headers=headers,
            json_body=json_body,
            timeout=timeout,
        )


# ---------------------------------------------------------------------------
# Git fakes (command runner / tempdir / cleanup)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(
        self, args: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _FakeCompletedProcess:
        self.calls.append(list(args))
        return _FakeCompletedProcess(
            args=list(args), returncode=0, stdout="", stderr=""
        )


class EventRunner:
    """Runner decorator that logs git-phase events into the shared order."""

    def __init__(self, inner: FakeRunner, events: list[str]) -> None:
        self._inner = inner
        self._events = events

    def __call__(self, args: list[str], **kwargs: Any) -> _FakeCompletedProcess:
        argv = list(args)
        if "clone" in argv:
            self._events.append("GIT clone")
        elif "--all" in argv:
            self._events.append("GIT push-branches")
        elif "--tags" in argv:
            self._events.append("GIT push-tags")
        else:
            self._events.append(f"GIT {' '.join(argv)}")
        return self._inner(argv, **kwargs)


class RecordingSink:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)


# ---------------------------------------------------------------------------
# Slice G end-to-end parity test
# ---------------------------------------------------------------------------


def test_orchestrator_end_to_end_parity_with_real_payload_shapes(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """One orchestrator run through slices A–F with real payload shapes."""
    monkeypatch.setattr(migration_mod, "_ISSUE_MUTATION_PAUSE_SECONDS", 0)

    events: list[str] = []

    # --- Source issues: scrambled created_at order (fresh #2 listed first) ---
    resumed_issue = {
        "number": 1,
        "title": "Old resumed issue",
        "body": "resumed body",
        "user": {"login": "alice"},
        "created_at": "2024-01-10T09:00:00Z",
        "labels": [],
        "comments": 0,  # real Forgejo shape: integer count, not a list
        "state": "open",
        "closed": False,
    }
    fresh_issue = {
        "number": 2,
        "title": "Fresh issue",
        "body": "fresh body text",
        "user": {"login": "bob"},
        "created_at": "2024-03-01T12:00:00Z",
        "labels": [
            {"name": "bug", "color": "d73a4a", "description": "A bug"},
            {"name": "feature", "description": "A feature"},  # no color -> "ededed"
        ],
        "comments": 4,  # real Forgejo shape: integer count, not a list
        "state": "open",
        "closed": False,
    }
    fresh_comments = [
        {
            "id": 11,
            "type": "Comment",
            "user": {"username": "alice"},
            "created_at": "2024-03-02T10:00:00Z",
            "body": "first!",
        },
        {
            "id": 12,
            "type": "Comment",
            "user": {"username": "carol"},
            "created_at": "2024-03-03T11:00:00Z",
            "body": "second thought",
        },
        {
            "id": 13,
            "type": "Comment",
            "user": {"username": "dave"},
            "created_at": "2024-03-04T12:00:00Z",
            "body": "third note",
        },
        {
            "id": 14,  # malformed: empty body -> filtered, counted, skipped
            "type": "Comment",
            "user": {"username": "mallory"},
            "created_at": "2024-03-05T13:00:00Z",
            "body": "",
        },
    ]

    # --- Codeberg transport: desc fetch, then issue pages, then comment pages --
    # (orchestrator order: pre-flight description BEFORE the issue listing)
    codeberg_transport = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload={"description": "source repo description"},
            ),
            FakeResponse(status_code=200, json_payload=[dict(fresh_issue), dict(resumed_issue)]),
            FakeResponse(status_code=200, json_payload=[]),
            FakeResponse(status_code=200, json_payload=[dict(c) for c in fresh_comments]),
            FakeResponse(status_code=200, json_payload=[]),
        ]
    )
    codeberg = CodebergClient(
        base_url="https://codeberg.org",
        owner="owner",
        repo="source",
        token="cb-token",
        transport=EventTransport(codeberg_transport, events, "CB"),
    )

    # --- GitHub transport: 404 check, create, label traffic, issue, comments --
    github_transport = FakeTransport(
        responses=[
            FakeResponse(status_code=404, json_payload={"message": "Not Found"}),
            FakeResponse(status_code=201, json_payload={"id": 7, "name": "target"}),
            FakeResponse(status_code=404, json_payload={"message": "Not Found"}),
            FakeResponse(status_code=201, json_payload={"name": "bug"}),
            FakeResponse(status_code=404, json_payload={"message": "Not Found"}),
            FakeResponse(status_code=201, json_payload={"name": "feature"}),
            FakeResponse(status_code=201, json_payload={"number": 101}),
            FakeResponse(status_code=201, json_payload={"id": 9001}),
            FakeResponse(status_code=201, json_payload={"id": 9002}),
            FakeResponse(status_code=201, json_payload={"id": 9003}),
        ]
    )
    github = GitHubClient(
        base_url="https://api.github.com",
        owner="owner",
        repo="target",
        token="gh-token",
        transport=EventTransport(github_transport, events, "GH"),
    )

    # --- Git mirror: scripted success (clone + branch push + tag push) ---
    runner = FakeRunner()
    cleanup_calls: list[str] = []

    def fake_tempdir_factory(prefix: str | None = None, **kwargs: Any) -> str:
        d = tmp_path / f"{prefix or 'f2gh'}-mirror"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def fake_cleanup(path: str, *args: Any, **kwargs: Any) -> None:
        cleanup_calls.append(path)

    git = GitMirror(
        source_url="https://codeberg.org/owner/source.git",
        target_url="https://github.com/owner/target.git",
        github_token="gh-token",
        command_runner=EventRunner(runner, events),
        tempdir_factory=fake_tempdir_factory,
        cleanup=fake_cleanup,
    )

    # --- State: pre-populated with the resumed issue; Reporter: recording ---
    state_path = tmp_path / "state.json"
    state = StateStore(state_path, source="owner/source", target="owner/target")
    state.save(False, False, {1: 99})

    out, err = RecordingSink(), RecordingSink()
    reporter = Reporter(output=out, error_output=err)

    repo = Repository(source="owner/source", target="owner/target", skip_git=False, yes=True)

    orchestrator = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
    )
    result = orchestrator.run()

    # --- create_issue body: attribution block + original body ---
    issue_posts = [
        c
        for c in github_transport.calls
        if c.method == "POST"
        and c.url == "https://api.github.com/repos/owner/target/issues"
    ]
    assert len(issue_posts) == 1, f"expected one issue-create POST, got {github_transport.calls!r}"
    issue_body = issue_posts[0].json_body["body"]
    expected_issue_body = format_issue_body(
        "owner/source", 2, "bob", "2024-03-01", "fresh body text"
    )
    assert issue_body == expected_issue_body
    assert "> **Migrated from Codeberg**" in issue_body
    assert "fresh body text" in issue_body
    assert issue_posts[0].json_body["labels"] == ["bug", "feature"]

    # --- ensure_label called twice with the right names/colors ---
    label_posts = [
        c
        for c in github_transport.calls
        if c.method == "POST"
        and c.url == "https://api.github.com/repos/owner/target/labels"
    ]
    assert len(label_posts) == 2, f"expected two label-create POSTs, got {github_transport.calls!r}"
    label_by_name = {c.json_body["name"]: c.json_body for c in label_posts}
    assert set(label_by_name) == {"bug", "feature"}
    assert label_by_name["bug"]["color"] == "d73a4a"
    assert label_by_name["feature"]["color"] == "ededed"

    # --- create_comment bodies: attribution block + original comment body ---
    comment_posts = [
        c
        for c in github_transport.calls
        if c.method == "POST"
        and c.url == "https://api.github.com/repos/owner/target/issues/101/comments"
    ]
    assert len(comment_posts) == 3, f"expected three comment POSTs, got {github_transport.calls!r}"
    expected_comment_bodies = [
        format_comment_body("alice", "2024-03-02", "first!"),
        format_comment_body("carol", "2024-03-03", "second thought"),
        format_comment_body("dave", "2024-03-04", "third note"),
    ]
    assert [c.json_body["body"] for c in comment_posts] == expected_comment_bodies

    # --- repo created with the fetched source description; no PATCH ---
    repo_posts = [
        c for c in github_transport.calls if c.url == "https://api.github.com/user/repos"
    ]
    assert len(repo_posts) == 1
    assert repo_posts[0].json_body["description"] == "source repo description"
    patches = [c for c in github_transport.calls if c.method == "PATCH"]
    assert patches == [], f"orchestrator must not PATCH the description: {patches!r}"

    # --- order: check -> description fetch -> create -> git -> issues ---
    idx_check = events.index("GH GET https://api.github.com/repos/owner/target")
    idx_desc = events.index("CB GET https://codeberg.org/api/v1/repos/owner/source")
    idx_create = events.index("GH POST https://api.github.com/user/repos")
    idx_clone = events.index("GIT clone")
    idx_issue = events.index("GH POST https://api.github.com/repos/owner/target/issues")
    assert idx_check < idx_desc < idx_create < idx_clone < idx_issue, (
        f"phase order broken: {events!r}"
    )
    assert events.index("GIT push-branches") < events.index("GIT push-tags")

    # --- result counters ---
    assert result.issues_attempted == 1
    assert result.issues_succeeded == 1
    assert result.issues_failed == 0
    assert result.comments_attempted == 4
    assert result.comments_succeeded == 3
    assert result.comments_failed == 0
    assert result.failures == []
    assert result.git["clone"] == "ok"
    assert result.git["push"] == "ok"

    # --- git_pushed persisted; skip lines in the recording sinks ---
    persisted = StateStore(state_path, source="owner/source", target="owner/target").load()
    assert persisted["git_pushed"] is True
    assert persisted["migrated"] == {1: 99, 2: 101}
    assert any("SKIP CB #1: already migrated" in line for line in out.lines), (
        f"resume skip line missing: {out.lines!r}"
    )
    assert any("SKIPPED [comment] CB #2:" in line for line in err.lines), (
        f"malformed-comment skip line missing: {err.lines!r}"
    )

    # --- resume: second orchestrator against the persisted state skips Git ---
    codeberg_transport2 = FakeTransport(
        responses=[
            FakeResponse(status_code=200, json_payload=[dict(fresh_issue), dict(resumed_issue)]),
            FakeResponse(status_code=200, json_payload=[]),
        ]
    )
    codeberg2 = CodebergClient(
        base_url="https://codeberg.org",
        owner="owner",
        repo="source",
        token="cb-token",
        transport=codeberg_transport2,
    )
    github_transport2 = FakeTransport(
        responses=[
            FakeResponse(
                status_code=200,
                json_payload={"name": "target", "open_issues_count": 0},
            ),
        ]
    )
    github2 = GitHubClient(
        base_url="https://api.github.com",
        owner="owner",
        repo="target",
        token="gh-token",
        transport=github_transport2,
    )
    runner2 = FakeRunner()
    git2 = GitMirror(
        source_url="https://codeberg.org/owner/source.git",
        target_url="https://github.com/owner/target.git",
        github_token="gh-token",
        command_runner=runner2,
        tempdir_factory=fake_tempdir_factory,
        cleanup=fake_cleanup,
    )
    state2 = StateStore(state_path, source="owner/source", target="owner/target")
    reporter2 = Reporter(output=RecordingSink(), error_output=RecordingSink())
    orchestrator2 = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg2,
        github=github2,
        git=git2,
        state=state2,
        reporter=reporter2,
    )
    result2 = orchestrator2.run()

    assert result2.git["clone"] == "skipped"
    assert result2.git["push"] == "skipped"
    assert runner2.calls == [], f"git phase must not run on resume: {runner2.calls!r}"
    assert result2.issues_attempted == 0
    assert len(github_transport2.calls) == 1  # check only: no create/issue traffic
