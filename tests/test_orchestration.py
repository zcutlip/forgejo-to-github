# RED class: C. Integration
#
# Orchestration tests (RED stage) for the package refactor described in
# plans/02-package-refactor-and-test-foundation.md. These tests assert the
# intended public contract of ``MigrationOrchestrator`` from
# ``forgejo_to_github.migration``:
#
# - the orchestrator is constructed with injected seams (repository/API/git/
#   state/report dependencies) and performs no network or subprocess work
#   of its own;
# - phase ordering is explicit: clone runs before issue work, and clone
#   failure is terminal while push failure is non-fatal;
# - within one issue, comments depend on issue creation; across issues,
#   one failure does not block subsequent issues.
#
# The orchestrator's ``run`` entry point returns a result object that is
# inspectable by these tests. Result fields used in assertions are part
# of the public contract (e.g., ``clone_status``, ``push_status``,
# ``issues_attempted``, ``issues_succeeded``, ``issues_failed``,
# ``issues_discovered``).
#
# RED-stage expectation: these tests fail via ``ImportError`` for the
# missing ``MigrationOrchestrator`` symbol or attribute on a result
# object. That is the contract under test.
#
# Approved API-alignment (2026-09-02): _FakeApi signatures updated to
# match concrete GitHubClient (title, body, labels)->int and
# (github_number, body)->int with recording plumbing adjusted.
# Behavioral assertions and contracts unchanged — not test weakening.
"""Orchestration tests for ``forgejo_to_github.migration``.

Approved API-alignment: fake collaborator signatures aligned to concrete
GitHubClient interfaces without weakening behavioral assertions.
"""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from forgejo_to_github.codeberg import CodebergClient
from forgejo_to_github.domain import Repository
from forgejo_to_github.formatting import format_comment_body
from forgejo_to_github.git import GitMirror
from forgejo_to_github.github import GitHubClient
from forgejo_to_github.migration import MigrationOrchestrator
from forgejo_to_github.reporting import Reporter
from forgejo_to_github.state import StateStore

# --- helpers ---------------------------------------------------------------


class _FakeRepo:
    """Minimal stand-in for a repository descriptor dependency."""

    def __init__(self, source: str, target: str) -> None:
        self.source = source
        self.target = target


class _FakeApi:
    """Records calls to the API client and yields canned issue payloads.

    The fake exposes only what the orchestrator contract requires: a way
    to verify the order in which the orchestrator reached for the API and
    a way to inject a per-issue failure on demand. It is intentionally
    not a faithful re-implementation of the real Codeberg/GitHub clients.

    Approved API-alignment: ``create_issue``/``create_comment`` now match
    the concrete ``GitHubClient`` signatures ``(title, body, labels)->int``
    and ``(github_number, body)->int``. Recording still exposes source
    numbers for ordering assertions; no compatibility branches for old
    dict payloads.
    """

    def __init__(
        self,
        issues: list[dict[str, Any]] | None = None,
        comments_by_issue: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.issues = list(issues or [])
        self.calls: list[tuple[str, ...]] = []
        self._fail_issue_numbers: set[int] = set()
        self._fail_comment_keys: set[tuple[int, int]] = set()
        self._github_to_source: dict[int, int] = {}
        self._comment_counters: dict[int, int] = {}
        self.comments_by_issue: dict[int, list[dict[str, Any]]] = dict(
            comments_by_issue or {}
        )
        self._fail_list_comments_numbers: set[int] = set()

    def list_issues(self) -> list[dict[str, Any]]:
        self.calls.append(("list_issues",))
        return list(self.issues)

    def list_comments(self, issue_id: int) -> list[dict[str, Any]]:
        self.calls.append(("list_comments", str(int(issue_id))))
        if int(issue_id) in self._fail_list_comments_numbers:
            raise RuntimeError(f"simulated list_comments failure for {issue_id}")
        return list(self.comments_by_issue.get(int(issue_id), []))

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        idx = len([c for c in self.calls if c[0] == "create_issue"])
        source_number = (
            int(self.issues[idx]["number"]) if idx < len(self.issues) else idx + 1
        )
        self.calls.append(("create_issue", str(source_number)))
        if source_number in self._fail_issue_numbers:
            raise RuntimeError(f"simulated create_issue failure for {source_number}")
        github_number = 100 + source_number
        self._github_to_source[github_number] = source_number
        return github_number

    def create_comment(self, github_number: int, body: str) -> int:
        source_number = self._github_to_source.get(
            int(github_number), int(github_number) - 100
        )
        comment_index = self._comment_counters.get(int(github_number), 0)
        self.calls.append(("create_comment", str(source_number), str(comment_index)))
        key = (source_number, comment_index)
        self._comment_counters[int(github_number)] = comment_index + 1
        if key in self._fail_comment_keys:
            raise RuntimeError(f"simulated comment failure for {key}")
        return 1000 + comment_index

    # --- failure injection (test-only) -----------------------------------

    def fail_on_create_issue(self, issue_number: int) -> None:
        self._fail_issue_numbers.add(int(issue_number))

    def fail_on_create_comment(self, issue_number: int, comment_index: int) -> None:
        self._fail_comment_keys.add((int(issue_number), int(comment_index)))

    def fail_on_list_comments(self, issue_number: int) -> None:
        self._fail_list_comments_numbers.add(int(issue_number))


class _FakeGit:
    """Stand-in for the Git mirror service.

    The orchestrator is expected to consult the Git seam for clone and
    push status. ``run_clone`` is terminal: a raise aborts the run.
    ``run_push`` is non-fatal: a raise is recorded but does not abort
    issue migration.
    """

    def __init__(self) -> None:
        self.clone_called = False
        self.push_called = False
        self.clone_error: Exception | None = None
        self.push_error: Exception | None = None

    def run_clone(self) -> None:
        self.clone_called = True
        if self.clone_error is not None:
            raise self.clone_error

    def run_push(self) -> None:
        self.push_called = True
        if self.push_error is not None:
            raise self.push_error


class _FakeState:
    """Records checkpoint calls in order.

    The orchestrator contract requires that the checkpoint only advances
    after the corresponding substep succeeds. ``record_issue`` is the
    end-of-issue checkpoint; ``record_comment`` is per-comment.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None]] = []

    def record_issue(self, source_number: int, github_number: int) -> None:
        self.events.append(("issue", int(source_number), int(github_number)))

    def record_comment(
        self, source_number: int, comment_index: int, github_comment_id: int
    ) -> None:
        self.events.append(("comment", int(source_number), int(comment_index)))

    def already_migrated(self, source_number: int) -> bool:
        for kind, src, _dst in self.events:
            if kind == "issue" and src == source_number:
                return True
        return False


class _FakeReport:
    """Captures messages handed to the reporter seam.

    The reporter seam is consulted by the orchestrator for human-readable
    progress and a final summary. The orchestrator's contract is that it
    does not format strings itself; it hands structured events to the
    reporter.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, ...]] = []

    def issue_started(self, source_number: int) -> None:
        self.events.append(("issue_started", str(source_number)))

    def issue_succeeded(self, source_number: int, github_number: int) -> None:
        self.events.append(("issue_succeeded", str(source_number), str(github_number)))

    def issue_failed(
        self, source_number: int, kind: str, message: str | None = None
    ) -> None:
        self.events.append(("issue_failed", str(source_number), kind, message))

    def comment_skipped(self, source_number: int, reason: str) -> None:
        self.events.append(("comment_skipped", str(source_number), reason))

    def git_phase_finished(self, status: str) -> None:
        self.events.append(("git_phase_finished", status))

    def summary(self, result: Any) -> None:
        # The reporter consumes the orchestrator's result object. The
        # test asserts that this happens exactly once per run.
        self.events.append(("summary",))


def _issue(number: int, *, title: str = "issue", comments: int = 0) -> dict[str, Any]:
    """Build a minimal Codeberg issue payload for fakes."""
    return {
        "number": int(number),
        "title": title,
        "comments": [
            {"index": i, "body": f"comment {i} for {number}"} for i in range(comments)
        ],
    }


def _build(
    *,
    issues: list[dict[str, Any]] | None = None,
    git: _FakeGit | None = None,
    state: _FakeState | None = None,
    api: _FakeApi | None = None,
    report: _FakeReport | None = None,
) -> tuple[MigrationOrchestrator, dict[str, Any]]:
    """Construct an orchestrator with the standard set of fakes.

    Returns the orchestrator together with the dictionary of fakes so
    that each test can poke at the right seam.
    """
    repo = _FakeRepo("owner/source", "owner/target")
    api = api if api is not None else _FakeApi(issues)
    git = git if git is not None else _FakeGit()
    state = state if state is not None else _FakeState()
    report = report if report is not None else _FakeReport()
    orchestrator = MigrationOrchestrator(
        repo=repo,
        api=api,
        git=git,
        state=state,
        report=report,
    )
    return orchestrator, {
        "repo": repo,
        "api": api,
        "git": git,
        "state": state,
        "report": report,
    }


# --- 1. constructor accepts injected dependencies ----------------------------


def test_orchestrator_constructor_accepts_injected_dependencies():
    """All five seams (repo, api, git, state, report) are constructor args.

    The refactor's contract is dependency injection: each seam must be
    supplied explicitly so that tests can replace any one of them with
    a fake without monkey-patching module-level globals.
    """
    orch, fakes = _build(issues=[])

    assert orch is not None
    # Touching each fake here documents that the test sees them all.
    assert fakes["repo"].source == "owner/source"
    assert fakes["api"] is not None
    assert fakes["git"] is not None
    assert fakes["state"] is not None
    assert fakes["report"] is not None


# --- 2. phase ordering: clone runs before any issue work --------------------


def test_clone_runs_before_any_issue_work():
    """The Git clone phase must complete before any API call to list issues."""
    api = _FakeApi(issues=[_issue(1, comments=1)])
    git = _FakeGit()
    state = _FakeState()
    report = _FakeReport()

    orch, _fakes = _build(api=api, git=git, state=state, report=report)

    orch.run()

    # The first API call must be a list, never a create.
    assert api.calls, "orchestrator must reach the API"
    assert api.calls[0][0] == "list_issues", (
        f"clone must precede the first issue-related API call; got {api.calls[0]!r}"
    )
    assert git.clone_called is True
    # And clone must precede any issue work in the timeline of events.
    assert any(kind == "issue" for kind, *_ in state.events)


# --- 3. clone failure is terminal: no issue API calls after it --------------


def test_clone_failure_is_terminal_and_skips_issue_migration():
    """A clone error must abort the run before any issue is created.

    The terminal-vs-non-fatal semantics are part of the contract: clone
    failure must not silently fall through to issue migration.
    """
    api = _FakeApi(issues=[_issue(1, comments=2), _issue(2, comments=1)])
    git = _FakeGit()
    git.clone_error = RuntimeError("simulated clone failure")
    state = _FakeState()
    report = _FakeReport()

    orch, _fakes = _build(api=api, git=git, state=state, report=report)

    with pytest.raises(RuntimeError):
        orch.run()

    create_calls = [c for c in api.calls if c[0] == "create_issue"]
    assert create_calls == [], (
        f"clone failure is terminal; no issue create must occur; got {create_calls!r}"
    )
    # State must not have advanced past the clone phase.
    assert state.events == [], (
        "no checkpoints may be recorded when clone fails terminally; "
        f"got {state.events!r}"
    )


# --- 4. push failure is non-fatal: issues still migrate --------------------


def test_push_failure_does_not_block_issue_migration():
    """A push failure must be recorded but issue migration must proceed.

    The orchestrator contract distinguishes push (non-fatal) from clone
    (terminal). This test asserts both the non-aborting behavior and
    that the failure is surfaced through the result so the reporter can
    summarize it.
    """
    api = _FakeApi(issues=[_issue(1, comments=1), _issue(2, comments=0)])
    git = _FakeGit()
    git.push_error = RuntimeError("simulated push failure")
    state = _FakeState()
    report = _FakeReport()

    orch, _fakes = _build(api=api, git=git, state=state, report=report)

    result = orch.run()

    # Push was attempted and issue migration still happened.
    assert git.push_called is True
    create_calls = [c for c in api.calls if c[0] == "create_issue"]
    assert len(create_calls) == 2, (
        f"push failure must not block issue creation; got {create_calls!r}"
    )
    # The result must carry the push status so the reporter can name it.
    assert hasattr(result, "push_status"), (
        "orchestrator result must expose push_status; result lacks it"
    )
    assert result.push_status != "ok", (
        f"push_status must reflect the failure, got {result.push_status!r}"
    )


# --- 5. per-issue dependency: create before comment and checkpoint --------


def test_create_issue_runs_before_comments_and_checkpoint():
    """Within one issue, comments and the issue checkpoint come after create.

    The orchestrator's per-issue ordering is part of the public
    contract: it must not post comments to a not-yet-created issue,
    and it must not checkpoint an issue whose creation did not succeed.
    """
    api = _FakeApi(
        issues=[_issue(1, comments=3), _issue(2, comments=1)],
        comments_by_issue={
            1: [
                {
                    "type": "Comment",
                    "user": {"username": "alice"},
                    "created_at": "2024-01-02T10:30:00Z",
                    "body": f"comment {i} for 1",
                }
                for i in range(3)
            ],
        },
    )
    state = _FakeState()
    report = _FakeReport()

    orch, _fakes = _build(api=api, state=state, report=report)

    orch.run()

    # For issue 1, create must come before any of its comments.
    _ = [
        c
        for c in api.calls
        if c[0] in {"create_issue", "create_comment"}
        and (len(c) < 2 or c[1] == "1" or (len(c) >= 3 and c[1] == "1"))
    ]
    create_index = next(
        i for i, c in enumerate(api.calls) if c[0] == "create_issue" and c[1] == "1"
    )
    comment_indices = [
        i for i, c in enumerate(api.calls) if c[0] == "create_comment" and c[1] == "1"
    ]
    assert comment_indices, "expected comments to be posted for issue 1"
    assert create_index < min(comment_indices), (
        "issue creation must precede comment posting for the same issue; "
        f"create_index={create_index}, comment_indices={comment_indices}"
    )

    # Checkpoint for issue 1 must occur after its create call.
    issue1_checkpoint = next(
        i for i, e in enumerate(state.events) if e[0] == "issue" and e[1] == 1
    )
    assert create_index < issue1_checkpoint, (
        "checkpoint for issue 1 must come after create_issue; "
        f"create_index={create_index}, checkpoint_index={issue1_checkpoint}"
    )


# --- 6. later issues continue after one issue failure ----------------------


def test_later_issue_continues_after_one_issue_creation_failure():
    """An issue-creation failure on issue N must not stop issue N+1.

    The orchestrator contract is to attempt every issue, accumulate
    per-issue outcomes, and never abort on a single issue failure.
    """
    api = _FakeApi(issues=[_issue(1), _issue(2), _issue(3)])
    api.fail_on_create_issue(2)
    state = _FakeState()
    report = _FakeReport()

    orch, _fakes = _build(api=api, state=state, report=report)

    result = orch.run()

    # All three issues were attempted in order.
    create_calls = [c for c in api.calls if c[0] == "create_issue"]
    assert [c[1] for c in create_calls] == ["1", "2", "3"], (
        f"each issue must be attempted even after a failure; got {create_calls!r}"
    )

    # Only the successful issues are checkpointed.
    checkpointed_sources = [e[1] for e in state.events if e[0] == "issue"]
    assert checkpointed_sources == [1, 3], (
        "only successfully-created issues may be checkpointed; got "
        f"{checkpointed_sources!r}"
    )

    # The result aggregates the failure so the reporter can name it.
    assert hasattr(result, "issues_attempted"), (
        "result must expose issues_attempted; missing"
    )
    assert hasattr(result, "issues_failed"), "result must expose issues_failed; missing"
    assert result.issues_attempted == 3
    assert result.issues_failed == 1


# ===========================================================================
# RED class: B. Boundary unit — dry-run boundary contract (append-only)
# ===========================================================================
#
# Contract (user-approved): dry run is read-only, NOT offline. A dry run
# must still perform GET-only discovery (target repository check, source
# issue listing) so the summary reports what *would* be migrated, while
# never spawning subprocesses and never persisting checkpoints.
#
# The current implementation short-circuits ``run()`` with zero requests
# when ``repo.dry_run`` is set (see ``migration.py`` Phase 1), so these
# tests are RED against that behavior. One primary failure reason per
# test; fully offline and deterministic — the transport and subprocess
# boundaries below are recording fakes and perform no real I/O.
#
# B.4 is intentionally GREEN at this stage only in its subprocess
# guard half (see that test's docstring); the state-write and
# report-count contracts are RED. The B.2 subprocess guard stays green
# in RED because the short-circuiting dry run allows no subprocess at
# all — that is a regression guard, not an implemented feature.


class _FakeResponse:
    """Minimal response-like object matching the Transport protocol."""

    def __init__(
        self,
        status_code: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code: int = status_code
        self.headers: dict[str, str] = headers if headers is not None else {}
        self._payload: Any = payload

    def json(self) -> Any:
        return self._payload


class _ScriptedDiscoveryTransport:
    """Recording Transport scripted for dry-run GET discovery.

    Routes the two GET endpoints the dry-run discovery phase is expected
    to consult: the GitHub target repository check and the Codeberg
    issues list (paginated: page 1 returns the scripted issues, page 2
    returns an empty page). Every request is recorded so tests can
    assert the method boundary. Non-GET requests are recorded and
    answered with a benign 201 so a broken dry run fails the method
    assertion rather than with an unrelated client error.
    """

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues: list[dict[str, Any]] = list(issues)
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append((method, url))
        if method != "GET":
            # Write paths must never be reached during a dry run; keep
            # the response plausible so the method assertion stays the
            # single failure reason.
            return _FakeResponse(201, {"number": 101, "id": 1001})
        if url.endswith("/repos/owner/target"):
            # GitHub target repository existence check.
            return _FakeResponse(200, {"name": "target"})
        if "/repos/owner/source/issues" in url and "/comments" not in url:
            page = int((params or {}).get("page", 1))
            return _FakeResponse(200, list(self.issues) if page == 1 else [])
        # Anything else (e.g. repo description GET) is inert.
        return _FakeResponse(200, {})


class _RecordingSink:
    """Recording output sink satisfying the Reporter Sink protocol."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)


def _build_dry_run(
    *,
    codeberg: Any,
    github: Any,
    git: Any,
    state: Any,
    reporter: Any,
) -> MigrationOrchestrator:
    """Construct an orchestrator over a dry-run ``Repository`` value."""
    return MigrationOrchestrator(
        repo=Repository(source="owner/source", target="owner/target", dry_run=True),
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
    )


# --- B.1 dry run issues GET-only discovery requests -------------------------


def test_dry_run_issues_only_get_requests() -> None:
    """Dry run performs read-only discovery: every HTTP request is a GET.

    The orchestrator must consult the discovery endpoints (GitHub target
    repository check, Codeberg issues list) via the real clients backed
    by a recording transport, and must never issue a POST/PATCH/PUT.
    """
    transport = _ScriptedDiscoveryTransport(issues=[_issue(1), _issue(2)])
    codeberg = CodebergClient(
        "https://codeberg.org", "owner", "source", None, transport=transport
    )
    github = GitHubClient(
        "https://api.github.com", "owner", "target", None, transport=transport
    )
    orch = _build_dry_run(
        codeberg=codeberg,
        github=github,
        git=_FakeGit(),
        state=_FakeState(),
        reporter=_FakeReport(),
    )

    orch.run()

    # Primary failure reason: the short-circuiting dry run makes zero
    # requests, so read-only discovery is not happening at all.
    get_calls = [call for call in transport.calls if call[0] == "GET"]
    assert get_calls, (
        "dry run must issue at least one GET discovery request; "
        f"recorded calls: {transport.calls!r}"
    )
    non_get = [call for call in transport.calls if call[0] != "GET"]
    assert non_get == [], (
        f"dry run is read-only; only GET requests are allowed; got {non_get!r}"
    )


# --- B.2 dry run never reaches the subprocess boundary ----------------------


def test_dry_run_makes_no_subprocess_calls() -> None:
    """Dry run must not clone or push: zero git subprocess invocations.

    This guard is intentionally green during RED: the current
    short-circuiting dry run allows no subprocess at all, so the
    assertion holds trivially. It is kept as a regression guard —
    once read-only discovery is implemented, it must keep holding
    (discovery issues GET requests only, never a git subprocess).
    """
    commands: list[list[str]] = []

    def recording_runner(argv: list[str], **kwargs: Any) -> Any:
        commands.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_tempdir(prefix: str | None = None, **kwargs: Any) -> str:
        # Recording-only: never creates a real directory.
        return "/nonexistent-fake-tempdir"

    git_mirror = GitMirror(
        source_url="https://codeberg.org/owner/source.git",
        target_url="https://github.com/owner/target.git",
        github_token="not-a-real-token",
        command_runner=recording_runner,
        tempdir_factory=fake_tempdir,
        cleanup=lambda path: None,
    )
    transport = _ScriptedDiscoveryTransport(issues=[_issue(1)])
    orch = _build_dry_run(
        codeberg=CodebergClient(
            "https://codeberg.org", "owner", "source", None, transport=transport
        ),
        github=GitHubClient(
            "https://api.github.com", "owner", "target", None, transport=transport
        ),
        git=git_mirror,
        state=_FakeState(),
        reporter=_FakeReport(),
    )

    orch.run()

    assert commands == [], f"dry run must not spawn subprocesses; got {commands!r}"


# --- B.3 dry run never persists the checkpoint ------------------------------


class _SaveSpyStateStore(StateStore):
    """Narrow spy over ``StateStore`` that records ``save`` calls.

    Overrides only ``save``; loading and all other behavior remain the
    real implementation's. No implementation-side compatibility hooks
    are assumed — this is a plain subclass, appropriate to the current
    ``StateStore.save`` signature.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.save_calls: list[tuple[bool, bool, int]] = []

    def save(
        self,
        repo_created: bool,
        git_pushed: bool,
        migrated: dict[int, int],
    ) -> None:
        self.save_calls.append((bool(repo_created), bool(git_pushed), len(migrated)))
        # Spy only: record and delegate nothing — a dry run must never
        # reach the real write path. Recording instead of delegating
        # also guarantees the on-disk file cannot change via this seam.


def test_dry_run_does_not_write_state(tmp_path: Path) -> None:
    """Dry run must not create or update the on-disk state checkpoint.

    Two assertions back the same invariant:

    - a valid pre-populated ``state.json`` remains byte-for-byte
      unchanged across the dry run; and
    - no ``StateStore.save`` call occurs during the run (observed via
      the ``_SaveSpyStateStore`` subclass).
    """
    state_path = tmp_path / "state.json"
    # Prepopulate a valid checkpoint via the real StateStore.write path
    # so the test exercises the "existing file stays untouched" path,
    # not just "no file created".
    seed_store = StateStore(state_path, "owner/source", "owner/target")
    seed_store.save(repo_created=True, git_pushed=True, migrated={7: 107, 9: 109})
    before: bytes = state_path.read_bytes()
    # The run itself observes the spy subclass: real load behavior, but
    # ``save`` is recorded and never reaches the write path.
    store = _SaveSpyStateStore(state_path, "owner/source", "owner/target")
    orch = _build_dry_run(
        codeberg=_FakeApi(issues=[]),
        github=_FakeApi(issues=[]),
        git=_FakeGit(),
        state=store,
        reporter=_FakeReport(),
    )

    orch.run()

    assert state_path.read_bytes() == before, (
        "dry run must leave an existing state checkpoint byte-for-byte "
        f"unchanged; before={before!r}, after={state_path.read_bytes()!r}"
    )
    assert store.save_calls == [], (
        "StateStore.save must not be called during a dry run; "
        f"recorded calls: {store.save_calls!r}"
    )


# --- B.4 dry-run summary reports the discovered issue count -----------------


def test_dry_run_reports_discovered_issue_count() -> None:
    """The dry-run summary must name the discovered count, not zero.

    The orchestrator discovers two source issues during dry-run GET
    discovery. The result contract keeps ``issues_attempted == 0``
    (discovery is not an attempt) and carries the found count in
    ``issues_discovered``. The reporter's dry-run template must
    surface that count as "would process N issues". The CLI owns the
    final summary emission, so this test drives ``render_final``
    exactly as the CLI does after ``run()``.
    """
    transport = _ScriptedDiscoveryTransport(issues=[_issue(1), _issue(2, comments=1)])
    codeberg = CodebergClient(
        "https://codeberg.org", "owner", "source", None, transport=transport
    )
    github = GitHubClient(
        "https://api.github.com", "owner", "target", None, transport=transport
    )
    sink = _RecordingSink()
    reporter = Reporter(output=sink, error_output=sink)
    orch = _build_dry_run(
        codeberg=codeberg,
        github=github,
        git=_FakeGit(),
        state=_FakeState(),
        reporter=reporter,
    )

    result = orch.run()
    reporter.render_final(result)

    # Discovery is not an attempt: a dry run never enters an issue.
    assert result.issues_attempted == 0, (
        "dry-run result must keep issues_attempted == 0; "
        f"got {result.issues_attempted!r}"
    )
    # The discovered count is carried separately from the attempt
    # counters and is the value the summary renders.
    assert result.issues_discovered == 2, (
        "dry-run result must carry the discovered source issue count "
        f"(2) in issues_discovered; got {getattr(result, 'issues_discovered', '<missing>')!r}"
    )

    joined = "\n".join(sink.lines)
    assert "would process 2 issues" in joined, (
        "dry-run summary must report the discovered issue count "
        '("would process 2 issues"), not zero; got:\n'
        f"{joined}"
    )


# ===========================================================================
# RED class: D. Informative dry-run preview (append-only)
# ===========================================================================
#
# Contract (user-approved, Option B): the dry-run summary must be an
# informative preview of what a real run would do — the target
# repository name, whether the target would be created or already
# exists, and the checkpoint status of the existing state file.
# Discovery facts are carried on the result via the approved
# ``DryRunDiscovery`` value (``result.discovery``) with fields
# ``target``, ``repo_exists``, ``comments_discovered``, ``state_path``,
# and ``state_migrated``. Rendering stays read-only: GET-only HTTP, no
# git subprocess, and the state file is loaded but never written.
#
# RED-stage expectation: both tests fail against the current
# implementation because ``MigrationResult`` carries no ``discovery``
# field and the dry-run template renders none of the new lines.


class _ScriptedPreviewTransport:
    """Recording transport scripted for the informative dry-run preview.

    Routes the GET endpoints the preview depends on, with a
    configurable target-repository existence (404 => missing, 200 =>
    existing). Every request is recorded so tests can assert the
    read-only method boundary; non-GET requests are answered with a
    benign 201 so the read-only assertion stays the single failure
    reason.
    """

    def __init__(
        self,
        issues: list[dict[str, Any]],
        *,
        target_exists: bool,
    ) -> None:
        self.issues: list[dict[str, Any]] = list(issues)
        self.target_exists: bool = target_exists
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append((method, url))
        if method != "GET":
            # Write paths must never be reached during a dry run.
            return _FakeResponse(201, {"number": 101, "id": 1001})
        if url.endswith("/repos/owner/target"):
            # GitHub target repository existence check.
            if self.target_exists:
                return _FakeResponse(200, {"name": "target"})
            return _FakeResponse(404, {"message": "Not Found"})
        if "/repos/owner/source/issues" in url and "/comments" not in url:
            page = int((params or {}).get("page", 1))
            return _FakeResponse(200, list(self.issues) if page == 1 else [])
        # Source repository description and anything else is inert.
        return _FakeResponse(200, {})


# --- D.1 dry-run summary previews the target-repository outcome -------------


def test_dry_run_summary_reports_missing_target_would_be_created() -> None:
    """A missing target (GET 404) previews creation and stays read-only.

    The summary must name the target repo and say it would be created.
    Only GET requests are issued and the git seam is never touched.
    """
    transport_missing = _ScriptedPreviewTransport(
        issues=[_issue(1)], target_exists=False
    )
    sink_missing = _RecordingSink()
    reporter_missing = Reporter(output=sink_missing, error_output=sink_missing)
    git_missing = _FakeGit()
    orch_missing = _build_dry_run(
        codeberg=CodebergClient(
            "https://codeberg.org", "owner", "source", None, transport=transport_missing
        ),
        github=GitHubClient(
            "https://api.github.com",
            "owner",
            "target",
            None,
            transport=transport_missing,
        ),
        git=git_missing,
        state=_FakeState(),
        reporter=reporter_missing,
    )

    result_missing = orch_missing.run()
    reporter_missing.render_final(result_missing)

    discovery_missing = getattr(result_missing, "discovery", None)
    assert discovery_missing is not None, (
        "dry-run result must carry the approved discovery facts "
        "(result.discovery is absent on MigrationResult); got "
        f"{discovery_missing!r}"
    )
    assert discovery_missing.target == "owner/target", (
        "discovery.target must be the target repository as owner/repo; "
        f"got {discovery_missing.target!r}"
    )
    assert discovery_missing.repo_exists is False, (
        "a 404 target check must record repo_exists=False; "
        f"got {discovery_missing.repo_exists!r}"
    )

    joined_missing = "\n".join(sink_missing.lines)
    assert "Target repo: owner/target" in joined_missing, (
        'dry-run summary must contain "Target repo: owner/target"; got:\n'
        f"{joined_missing}"
    )
    assert "Repo: would be created" in joined_missing, (
        'dry-run summary must contain "Repo: would be created" for a '
        f"missing target; got:\n{joined_missing}"
    )

    # Read-only: GET-only transport, git seam untouched.
    non_get = [c for c in transport_missing.calls if c[0] != "GET"]
    assert non_get == [], f"dry run is read-only; only GET allowed; got {non_get!r}"
    assert git_missing.clone_called is False
    assert git_missing.push_called is False


def test_dry_run_summary_reports_existing_target_repo_existing() -> None:
    """An existing target (GET 200) previews the repo as existing."""
    transport_existing = _ScriptedPreviewTransport(
        issues=[_issue(1)], target_exists=True
    )
    sink_existing = _RecordingSink()
    reporter_existing = Reporter(output=sink_existing, error_output=sink_existing)
    orch_existing = _build_dry_run(
        codeberg=CodebergClient(
            "https://codeberg.org",
            "owner",
            "source",
            None,
            transport=transport_existing,
        ),
        github=GitHubClient(
            "https://api.github.com",
            "owner",
            "target",
            None,
            transport=transport_existing,
        ),
        git=_FakeGit(),
        state=_FakeState(),
        reporter=reporter_existing,
    )

    result_existing = orch_existing.run()
    reporter_existing.render_final(result_existing)

    discovery_existing = getattr(result_existing, "discovery", None)
    assert discovery_existing is not None, (
        "dry-run result must carry the approved discovery facts "
        "(result.discovery is absent on MigrationResult); got "
        f"{discovery_existing!r}"
    )
    assert discovery_existing.repo_exists is True, (
        "a 200 target check must record repo_exists=True; "
        f"got {discovery_existing.repo_exists!r}"
    )

    joined_existing = "\n".join(sink_existing.lines)
    assert "Repo: existing" in joined_existing, (
        'dry-run summary must contain "Repo: existing" for an existing '
        f"target; got:\n{joined_existing}"
    )


# --- D.1.b dry-run discovery tolerates numeric Codeberg comment counts -----
#
# Contract (real Codeberg API shape, see issue payload observations):
# the Codeberg ``/repos/{owner}/{repo}/issues`` listing includes a
# ``"comments"`` field that is an INTEGER count of comments on the
# issue — not a list of comment payloads. The dry-run discovery
# phase must coerce that numeric count into the
# ``discovery.comments_discovered`` total rather than calling ``len()``
# on an int (which raises ``TypeError``). Issue migration that follows
# must still pull comments from the per-issue comments endpoint, so
# only the discovery counter is at risk; this test stays at the dry-run
# boundary, never reaches the per-issue migration path, and stays
# offline.


def test_dry_run_handles_numeric_codeberg_comment_count() -> None:
    """Dry-run discovery must accept numeric ``"comments"`` from Codeberg.

    A Codeberg issue with ``"comments": 3`` (the real API shape) must
    not crash dry-run discovery. The discovered count must include the
    integer 3 in ``discovery.comments_discovered`` and the rendered
    summary must surface it as ``Comments: would post 3``.

    Primary failure reason: the current dry-run discovery does
    ``len(issue.get("comments") or [])``, which raises ``TypeError`` on
    an integer. The first assertion isolates that single failure so the
    GREEN implementation can be evaluated against a single, focused
    behavior delta.
    """
    # Real Codeberg API shape: ``comments`` is an integer count, not a
    # list. The issue has no embedded comment list — the per-issue
    # comments endpoint is fetched separately during migration, never
    # reached during dry-run discovery.
    numeric_comments_issue: dict[str, Any] = {
        "number": 1,
        "title": "issue with numeric comments",
        "comments": 3,
    }
    transport = _ScriptedPreviewTransport(
        issues=[numeric_comments_issue], target_exists=True
    )
    sink = _RecordingSink()
    reporter = Reporter(output=sink, error_output=sink)
    orch = _build_dry_run(
        codeberg=CodebergClient(
            "https://codeberg.org", "owner", "source", None, transport=transport
        ),
        github=GitHubClient(
            "https://api.github.com", "owner", "target", None, transport=transport
        ),
        git=_FakeGit(),
        state=_FakeState(),
        reporter=reporter,
    )

    result = orch.run()
    reporter.render_final(result)

    # Discovery must not have crashed on ``len(int)`` — the counter is
    # the primary contract under test.
    discovery = getattr(result, "discovery", None)
    assert discovery is not None, (
        "dry-run result must carry the approved discovery facts "
        "(result.discovery is absent on MigrationResult); got "
        f"{discovery!r}"
    )
    assert discovery.comments_discovered == 3, (
        "dry-run discovery must read numeric ``comments`` from a "
        "Codeberg issue listing as the integer count (3); got "
        f"{discovery.comments_discovered!r}"
    )

    joined = "\n".join(sink.lines)
    assert "Comments: would post 3" in joined, (
        'dry-run summary must contain "Comments: would post 3" when '
        "the source listing reports a numeric comment count of 3; "
        f"got:\n{joined}"
    )


# --- D.2 dry-run summary previews the checkpoint status ---------------------


def test_dry_run_summary_reports_state_checkpoint(tmp_path: Path) -> None:
    """The dry-run summary must preview the checkpoint status.

    A valid pre-populated ``state.json`` holding one migrated mapping
    is loaded read-only during discovery and previewed as
    ``State: <path> (1 checkpointed)``. The run must not write or
    mutate the file: its bytes remain unchanged and ``StateStore.save``
    is never called.
    """
    state_path = tmp_path / "state.json"
    # Prepopulate a valid checkpoint via the real StateStore write path.
    seed_store = StateStore(state_path, "owner/source", "owner/target")
    seed_store.save(repo_created=True, git_pushed=True, migrated={1: 101})
    before: bytes = state_path.read_bytes()

    # The run observes the spy subclass: real load behavior, but save
    # is recorded and never reaches the write path.
    store = _SaveSpyStateStore(state_path, "owner/source", "owner/target")
    transport = _ScriptedPreviewTransport(issues=[_issue(1)], target_exists=True)
    sink = _RecordingSink()
    reporter = Reporter(output=sink, error_output=sink)
    orch = _build_dry_run(
        codeberg=CodebergClient(
            "https://codeberg.org", "owner", "source", None, transport=transport
        ),
        github=GitHubClient(
            "https://api.github.com", "owner", "target", None, transport=transport
        ),
        git=_FakeGit(),
        state=store,
        reporter=reporter,
    )

    result = orch.run()
    reporter.render_final(result)

    discovery = getattr(result, "discovery", None)
    assert discovery is not None, (
        "dry-run result must carry the approved discovery facts "
        "(result.discovery is absent on MigrationResult); got "
        f"{discovery!r}"
    )
    assert discovery.state_migrated == 1, (
        "discovery.state_migrated must count the pre-populated "
        f"checkpoint (1); got {discovery.state_migrated!r}"
    )
    assert str(discovery.state_path) == str(state_path), (
        "discovery.state_path must be the state file path; "
        f"got {discovery.state_path!r}"
    )

    joined = "\n".join(sink.lines)
    assert str(state_path) in joined, (
        f"dry-run summary must report the state file path; got:\n{joined}"
    )
    assert "1 checkpointed" in joined, (
        f'dry-run summary must contain "1 checkpointed"; got:\n{joined}'
    )

    # Read-only state: file byte-identical, no save calls.
    assert state_path.read_bytes() == before, (
        "dry run must leave an existing state checkpoint byte-for-byte "
        f"unchanged; before={before!r}, after={state_path.read_bytes()!r}"
    )
    assert store.save_calls == [], (
        "StateStore.save must not be called during a dry run; "
        f"recorded calls: {store.save_calls!r}"
    )


# ===========================================================================
# RED class: E. Preflight target-repository phase (append-only)
# ===========================================================================
#
# Contract (locked in ``plans/02-package-refactor-and-test-foundation/
# audit-remediation.md`` §4.1/§4.5, Slice A): before any git or issue
# work, the orchestrator performs a preflight phase against the GitHub
# target:
#
# - ``github.check_repository_exists()`` decides whether the target
#   exists.
# - When the target is missing, the orchestrator resolves the
#   description (explicit ``repo.description`` wins; otherwise
#   ``codeberg.get_repository_description()``; on fetch failure the
#   ``"Migrated from Codeberg"`` fallback), confirms via the injected
#   ``prompter`` seam (locked signature ``prompter(prompt: str,
#   default: bool) -> bool``), and creates the target with
#   ``github.create_repository(name, description, public)`` — the
#   description folded into the create payload.
# - The orchestrator never calls ``update_repository_description``:
#   neither after creation nor for an existing target, regardless of
#   whether ``--description`` was supplied.
#
# RED-stage expectation: all three tests fail because the current
# orchestrator has no preflight phase — the constructor does not accept
# a ``prompter`` seam and ``run()`` never consults
# ``check_repository_exists`` / ``create_repository`` on the normal
# (non-dry-run) path.


class _FakePrompter:
    """Recording prompter seam matching the locked callable contract.

    The locked prompter signature is ``prompter(prompt: str,
    default: bool) -> bool`` (audit-remediation.md §4.5). The fake
    returns a scripted answer and records every prompt it is asked.
    """

    def __init__(self, answer: bool = True) -> None:
        self.answer = answer
        self.prompts: list[tuple[str, bool]] = []

    def __call__(self, prompt: str, default: bool) -> bool:
        self.prompts.append((prompt, default))
        return self.answer


class _FakeGitHub:
    """Recording fake for the GitHub seam (preflight phase).

    Mirrors the concrete ``GitHubClient`` signatures:
    ``check_repository_exists() -> dict | None``,
    ``create_repository(name, description, public) -> dict``, and
    ``update_repository_description(description) -> None``.
    """

    def __init__(self, repo: dict[str, Any] | None = None) -> None:
        self.repo = repo
        self.calls: list[tuple[str, ...]] = []
        self.created: list[tuple[str, str | None, bool]] = []

    def check_repository_exists(self) -> dict[str, Any] | None:
        self.calls.append(("check_repository_exists",))
        return self.repo

    def create_repository(
        self, name: str, description: str | None, public: bool
    ) -> dict[str, Any]:
        self.calls.append(("create_repository", name, str(description), str(public)))
        self.created.append((name, description, public))
        return {"name": name}

    def update_repository_description(self, description: str) -> None:
        self.calls.append(("update_repository_description", description))


class _FakeCodeberg:
    """Recording fake for the Codeberg seam (preflight phase).

    Mirrors the concrete ``CodebergClient`` signatures:
    ``list_issues(state="all") -> list[dict]`` and
    ``get_repository_description() -> str``.
    """

    def __init__(
        self,
        issues: list[dict[str, Any]] | None = None,
        description: str = "",
        description_error: Exception | None = None,
    ) -> None:
        self.issues = list(issues or [])
        self.description = description
        self.description_error = description_error
        self.calls: list[tuple[str, ...]] = []

    def list_issues(self, state: str = "all") -> list[dict[str, Any]]:
        self.calls.append(("list_issues", state))
        return list(self.issues)

    def get_repository_description(self) -> str:
        self.calls.append(("get_repository_description",))
        if self.description_error is not None:
            raise self.description_error
        return self.description


def _build_preflight(
    *,
    github: _FakeGitHub,
    codeberg: _FakeCodeberg,
    prompter: _FakePrompter,
    repo: Repository | None = None,
    git: _FakeGit | None = None,
    state: _FakeState | None = None,
    report: _FakeReport | None = None,
) -> MigrationOrchestrator:
    """Construct an orchestrator with preflight fakes and a prompter seam.

    The ``prompter`` keyword is part of the locked preflight contract
    (``prompter: Callable[[str, bool], bool] | None = None``); the
    current constructor does not accept it, which is the RED failure
    these tests are written against.
    """
    if repo is None:
        repo = Repository(source="owner/source", target="owner/target")
    return MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git if git is not None else _FakeGit(),
        state=state if state is not None else _FakeState(),
        reporter=report if report is not None else _FakeReport(),
        prompter=prompter,
    )


# --- E.1 missing target is created with the source description -------------


def test_orchestrator_creates_target_when_missing() -> None:
    """A missing target (check returns None) is created with the source description.

    The preflight phase must fetch the source description, confirm via
    the prompter seam, and create the target repository with that
    description folded into the create payload. It must not follow up
    with a description PATCH, and the run must continue into issue
    migration (not aborted).
    """
    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Test description")
    prompter = _FakePrompter(answer=True)

    orch = _build_preflight(github=github, codeberg=codeberg, prompter=prompter)

    result = orch.run()

    # Preflight consulted the target check and the source description.
    assert ("check_repository_exists",) in github.calls, (
        "preflight must check whether the target exists; "
        f"recorded calls: {github.calls!r}"
    )
    assert ("get_repository_description",) in codeberg.calls, (
        "a missing target without an explicit description must fetch "
        f"the source description; recorded calls: {codeberg.calls!r}"
    )
    create_call = ("create_repository", "target", "Test description", "False")
    assert create_call in github.calls, (
        "a missing target must be created with the source description; "
        f"recorded calls: {github.calls!r}"
    )
    # The description is folded into the create payload; no follow-up PATCH.
    assert not any(c[0] == "update_repository_description" for c in github.calls), (
        "the description must be set at creation time, not via a "
        f"follow-up PATCH; recorded calls: {github.calls!r}"
    )
    # The run is not aborted: issue migration proceeded.
    assert result.issues_attempted == 1, (
        "a confirmed preflight must not abort the run; issue migration "
        f"must proceed; issues_attempted={result.issues_attempted!r}"
    )
    assert not getattr(result, "aborted", False), (
        "a confirmed preflight must not mark the result aborted; "
        f"result.aborted={getattr(result, 'aborted', '<missing>')!r}"
    )


# --- E.2 existing target: neither created nor description-PATCHed ----------


def test_orchestrator_does_not_touch_description_when_target_exists() -> None:
    """An existing target is neither created nor description-PATCHed.

    When ``check_repository_exists`` returns a repo dict, the preflight
    phase must leave the target alone: no ``create_repository`` and no
    ``update_repository_description``, even when the run carries an
    explicit ``--description``.
    """
    github = _FakeGitHub(repo={"name": "target", "open_issues_count": 0})
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Source description")
    prompter = _FakePrompter(answer=True)
    repo = Repository(
        source="owner/source",
        target="owner/target",
        description="Explicit description",
    )

    orch = _build_preflight(
        github=github, codeberg=codeberg, prompter=prompter, repo=repo
    )

    result = orch.run()

    assert ("check_repository_exists",) in github.calls, (
        "preflight must check whether the target exists; "
        f"recorded calls: {github.calls!r}"
    )
    assert not any(c[0] == "create_repository" for c in github.calls), (
        f"an existing target must not be created; recorded calls: {github.calls!r}"
    )
    assert not any(c[0] == "update_repository_description" for c in github.calls), (
        "an existing target's description must not be touched, even "
        f"with an explicit --description; recorded calls: {github.calls!r}"
    )
    # The run is not aborted: issue migration proceeded.
    assert result.issues_attempted == 1, (
        "an existing target must not abort the run; issue migration "
        f"must proceed; issues_attempted={result.issues_attempted!r}"
    )


# --- E.3 source description fetch failure falls back ------------------------


def test_orchestrator_uses_fallback_description_on_source_fetch_failure() -> None:
    """A failing source description fetch falls back to the default.

    When ``codeberg.get_repository_description`` raises, the preflight
    phase must not abort: it creates the missing target with the
    ``"Migrated from Codeberg"`` fallback description.
    """
    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(
        issues=[_issue(1)],
        description_error=requests.HTTPError(
            "simulated source description fetch failure"
        ),
    )
    prompter = _FakePrompter(answer=True)

    orch = _build_preflight(github=github, codeberg=codeberg, prompter=prompter)

    result = orch.run()

    assert ("get_repository_description",) in codeberg.calls, (
        "a missing target without an explicit description must attempt "
        f"the source description fetch; recorded calls: {codeberg.calls!r}"
    )
    fallback_call = (
        "create_repository",
        "target",
        "Migrated from Codeberg",
        "False",
    )
    assert fallback_call in github.calls, (
        "a source description fetch failure must fall back to "
        f'"Migrated from Codeberg"; recorded calls: {github.calls!r}'
    )
    # The run is not aborted: issue migration proceeded.
    assert result.issues_attempted == 1, (
        "a description fetch failure must not abort the run; issue "
        f"migration must proceed; issues_attempted={result.issues_attempted!r}"
    )


# --- E.4 explicit description wins over Codeberg description --------------


def test_orchestrator_uses_explicit_description_when_provided() -> None:
    """An explicit --description wins; the Codeberg fetch is not called.

    When ``repo.description`` is non-empty and the target is missing,
    the orchestrator must use the explicit description in the
    ``create_repository`` payload and must NOT call
    ``codeberg.get_repository_description()``.
    """
    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Codeberg description")
    prompter = _FakePrompter(answer=True)
    repo = Repository(
        source="owner/source",
        target="owner/target",
        description="Explicit description",
    )

    orch = _build_preflight(
        github=github, codeberg=codeberg, prompter=prompter, repo=repo
    )

    orch.run()

    create_call = (
        "create_repository",
        "target",
        "Explicit description",
        "False",
    )
    assert create_call in github.calls, (
        "the explicit description must be used in the create payload; "
        f"recorded calls: {github.calls!r}"
    )
    assert not any(c[0] == "get_repository_description" for c in codeberg.calls), (
        "an explicit description must skip the Codeberg description fetch; "
        f"recorded calls: {codeberg.calls!r}"
    )


# --- E.5 prompts ----------------------------------------------------------


def test_orchestrator_prompts_before_creating_target() -> None:
    """The prompter is called once before create_repository when target missing."""
    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Test")
    prompter = _FakePrompter(answer=True)

    orch = _build_preflight(github=github, codeberg=codeberg, prompter=prompter)

    orch.run()

    assert len(prompter.prompts) == 1, (
        "the prompter must be called exactly once before creating the "
        f"target; prompts={prompter.prompts!r}"
    )


def test_orchestrator_skips_prompts_when_yes_flag_set() -> None:
    """repo.yes=True bypasses the prompter; creation proceeds directly."""
    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Test")
    prompter = _FakePrompter(answer=True)
    repo = Repository(source="owner/source", target="owner/target", yes=True)

    orch = _build_preflight(
        github=github, codeberg=codeberg, prompter=prompter, repo=repo
    )

    orch.run()

    assert len(prompter.prompts) == 0, (
        f"repo.yes=True must bypass the prompter; prompts={prompter.prompts!r}"
    )
    assert any(c[0] == "create_repository" for c in github.calls), (
        "creation must still proceed when --yes is set; "
        f"recorded calls: {github.calls!r}"
    )


def test_orchestrator_prompts_when_target_has_existing_issues() -> None:
    """An existing target with open issues triggers a warning prompt."""
    github = _FakeGitHub(repo={"name": "target", "open_issues_count": 5})
    codeberg = _FakeCodeberg(issues=[_issue(1)])
    prompter = _FakePrompter(answer=True)
    repo = Repository(source="owner/source", target="owner/target", yes=False)

    orch = _build_preflight(
        github=github, codeberg=codeberg, prompter=prompter, repo=repo
    )

    orch.run()

    assert len(prompter.prompts) == 1, (
        "a target with existing issues must trigger a warning prompt; "
        f"prompts={prompter.prompts!r}"
    )
    assert not any(c[0] == "create_repository" for c in github.calls), (
        f"an existing target must not be created; recorded calls: {github.calls!r}"
    )


def test_orchestrator_aborts_when_prompter_returns_false() -> None:
    """A denied prompt aborts: no create_repository, no create_issue."""
    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Test")
    prompter = _FakePrompter(answer=False)

    orch = _build_preflight(github=github, codeberg=codeberg, prompter=prompter)

    result = orch.run()

    assert not any(c[0] == "create_repository" for c in github.calls), (
        f"a denied prompt must not create the target; recorded calls: {github.calls!r}"
    )
    assert not any(c[0] == "create_issue" for c in github.calls), (
        f"a denied prompt must not create any issues; recorded calls: {github.calls!r}"
    )
    assert getattr(result, "aborted", False) is True, (
        "a denied prompt must set result.aborted=True; "
        f"result.aborted={getattr(result, 'aborted', '<missing>')!r}"
    )


def test_orchestrator_with_null_prompter_treats_prompts_as_denied() -> None:
    """prompter=None (the default) is deny-by-default: no mutations occur."""
    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Test")

    # prompter defaults to None in _build_preflight's signature — but since
    # _FakePrompter is the type hint, pass None explicitly.
    repo = Repository(source="owner/source", target="owner/target")
    orch = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=_FakeGit(),
        state=_FakeState(),
        reporter=_FakeReport(),
        prompter=None,
    )

    result = orch.run()

    assert not any(c[0] == "create_repository" for c in github.calls), (
        f"prompter=None must deny creation; recorded calls: {github.calls!r}"
    )
    assert getattr(result, "aborted", False) is True, (
        "prompter=None must set result.aborted=True; "
        f"result.aborted={getattr(result, 'aborted', '<missing>')!r}"
    )


# --- E.6 phase ordering ---------------------------------------------------


def test_orchestrator_runs_preflight_before_git_phase() -> None:
    """Preflight (check + create) runs before git clone, which runs before issues.

    The call order must be:
        check_repository_exists → create_repository → git clone → list_issues
    """
    order: list[str] = []

    github = _FakeGitHub(repo=None)
    codeberg = _FakeCodeberg(issues=[_issue(1)], description="Test")

    # Wrap the fakes to record into the shared order list.
    original_check = github.check_repository_exists
    original_create = github.create_repository

    def ordered_check() -> dict[str, Any] | None:
        order.append("check_repository_exists")
        return original_check()

    def ordered_create(
        name: str, description: str | None, public: bool
    ) -> dict[str, Any]:
        order.append("create_repository")
        return original_create(name, description, public)

    github.check_repository_exists = ordered_check  # type: ignore[method-assign]
    github.create_repository = ordered_create  # type: ignore[method-assign]

    original_list = codeberg.list_issues

    def ordered_list(state: str = "all") -> list[dict[str, Any]]:
        order.append("list_issues")
        return original_list(state)

    codeberg.list_issues = ordered_list  # type: ignore[method-assign]

    class _OrderedGit:
        def clone(self) -> str:
            order.append("git.clone")
            return "/tmp/fake-clone"

        def push_branches(self, local_path: str) -> None:
            pass

        def push_tags(self, local_path: str) -> None:
            pass

        def cleanup(self, local_path: str) -> None:
            pass

    prompter = _FakePrompter(answer=True)
    repo = Repository(source="owner/source", target="owner/target")
    orch = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=_OrderedGit(),
        state=_FakeState(),
        reporter=_FakeReport(),
        prompter=prompter,
    )

    orch.run()

    assert order == [
        "check_repository_exists",
        "create_repository",
        "git.clone",
        "list_issues",
    ], (
        "preflight must run before git clone, which must run before "
        f"issue listing; actual order={order!r}"
    )


# ===========================================================================
# Pacing between issue mutations (append-only)
# ===========================================================================
#
# Contract (Slice B): the orchestrator paces successive GitHub
# issue-mutation calls (create_issue / create_comment / close_issue)
# with a 0.3 s pause (module constant
# ``_ISSUE_MUTATION_PAUSE_SECONDS`` in
# ``forgejo_to_github.migration``, via module-level ``time.sleep``
# so tests can mock it), restoring the baseline ``main:f2gh.py``
# behavior of sleeping 0.3 s between consecutive issue mutations.
#
# RED-stage expectation: this test fails because the orchestrator
# currently fires issue-mutation calls as fast as possible with no
# pacing sleep at all.


def test_orchestrator_pauses_between_issue_mutation_calls(monkeypatch: Any) -> None:
    """Successive GitHub issue mutations are paced with a 0.3 s pause.

    The orchestrator migrates two issues (with comments and a close)
    with ``skip_git=True``. Every pair of successive issue-mutation
    calls (create_issue / create_comment / close_issue) must be
    separated by a ``time.sleep`` pacing call, and every pacing call
    must pass the 0.3 s module constant.
    """
    import forgejo_to_github.migration as migration_mod

    sleep_calls: list[Any] = []
    timeline: list[tuple[str, Any]] = []

    def _recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        timeline.append(("sleep", seconds))

    # Mock the module-level pacing seam. Before GREEN adds
    # ``import time`` to the migration module, install a stub so the
    # RED failure is the pacing assertion below (no pacing exists
    # yet), not a missing-attribute error at mock time.
    if getattr(migration_mod, "time", None) is None:
        monkeypatch.setattr(
            migration_mod,
            "time",
            SimpleNamespace(sleep=_recording_sleep),
            raising=False,
        )
    else:
        monkeypatch.setattr(migration_mod.time, "sleep", _recording_sleep)

    closed_issue = _issue(2, comments=1)
    closed_issue["closed"] = True
    api = _FakeApi(issues=[_issue(1, comments=1), closed_issue])

    # Wrap the mutation seam so the shared timeline interleaves
    # mutations with pacing sleeps in call order.
    real_create_issue = api.create_issue
    real_create_comment = api.create_comment

    def _timed_create_issue(title: str, body: str, labels: list[str]) -> int:
        github_number = real_create_issue(title, body, labels)
        timeline.append(("mutation", "create_issue"))
        return github_number

    def _timed_create_comment(github_number: int, body: str) -> int:
        comment_id = real_create_comment(github_number, body)
        timeline.append(("mutation", "create_comment"))
        return comment_id

    def _timed_close_issue(github_number: int) -> None:
        timeline.append(("mutation", "close_issue"))
        api.calls.append(("close_issue", str(int(github_number) - 100)))

    api.create_issue = _timed_create_issue  # type: ignore[method-assign]
    api.create_comment = _timed_create_comment  # type: ignore[method-assign]
    api.close_issue = _timed_close_issue  # type: ignore[attr-defined]

    repo = Repository(source="owner/source", target="owner/target", skip_git=True)
    orch = MigrationOrchestrator(
        repo=repo,
        api=api,
        git=_FakeGit(),
        state=_FakeState(),
        report=_FakeReport(),
    )

    orch.run()

    mutation_indices = [i for i, event in enumerate(timeline) if event[0] == "mutation"]
    assert len(mutation_indices) >= 2, (
        "expected at least two GitHub issue-mutation calls "
        f"(create_issue/create_comment/close_issue); timeline={timeline!r}"
    )

    pacing: list[Any] = []
    for first, second in pairwise(mutation_indices):
        gap = [e for e in timeline[first + 1 : second] if e[0] == "sleep"]
        assert gap, (
            "expected a pacing sleep between successive GitHub "
            "issue-mutation calls; "
            f"gap between timeline[{first}]={timeline[first]!r} and "
            f"timeline[{second}]={timeline[second]!r} has no sleep; "
            f"timeline={timeline!r}"
        )
        pacing.extend(seconds for _, seconds in gap)

    assert pacing, (
        "expected at least one pacing sleep between successive GitHub "
        f"issue-mutation calls; timeline={timeline!r}"
    )
    expected_pause = getattr(migration_mod, "_ISSUE_MUTATION_PAUSE_SECONDS", 0.3)
    assert expected_pause == 0.3, (
        f"the pacing constant must be 0.3 s; got {expected_pause!r}"
    )
    assert all(seconds == expected_pause for seconds in pacing), (
        "every pacing call must pass the 0.3 s module constant; "
        f"pacing={pacing!r}, expected={expected_pause!r}"
    )


# ===========================================================================
# Issue fidelity: ordering, labels, attribution (append-only)
# ===========================================================================
#
# Contract (Slice C): the per-issue loop enriches each migrated issue —
# issues are migrated in ``created_at`` ascending order, Codeberg label
# dicts are mapped to plain name strings, each label is ensured via
# ``github.ensure_label`` (defaulting a missing color to ``"ededed"``)
# before ``create_issue``, and the body is wrapped with the
# ``format_issue_body`` attribution block.
#
# RED-stage expectation: all five tests fail because the current loop
# iterates API order, stringifies label dicts, never calls
# ``ensure_label``, and passes the raw body through.


def test_orchestrator_migrates_issues_in_creation_date_order() -> None:
    """Source issues scrambled vs ``created_at`` are created oldest-first."""
    issues = [
        {"number": 1, "title": "newest", "created_at": "2024-03-01T10:00:00Z"},
        {"number": 2, "title": "oldest", "created_at": "2024-01-01T10:00:00Z"},
        {"number": 3, "title": "middle", "created_at": "2024-02-01T10:00:00Z"},
    ]
    api = _FakeApi(issues=issues)
    titles: list[str] = []
    real_create_issue = api.create_issue

    def _recording_create_issue(title: str, body: str, labels: list[str]) -> int:
        titles.append(title)
        return real_create_issue(title, body, labels)

    api.create_issue = _recording_create_issue  # type: ignore[method-assign]

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    orch.run()

    assert titles == ["oldest", "middle", "newest"], (
        f"issues must be migrated in created_at ascending order; got {titles!r}"
    )


def test_orchestrator_passes_label_names_not_dicts() -> None:
    """Codeberg label dicts must reach ``create_issue`` as plain names."""
    issue = _issue(1)
    issue["labels"] = [{"name": "bug", "color": "ff0000"}, {"name": "ui"}]
    api = _FakeApi(issues=[issue])
    captured: dict[str, Any] = {}
    real_create_issue = api.create_issue

    def _recording_create_issue(title: str, body: str, labels: list[str]) -> int:
        captured["labels"] = list(labels)
        return real_create_issue(title, body, labels)

    api.create_issue = _recording_create_issue  # type: ignore[method-assign]

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    orch.run()

    assert captured.get("labels") == ["bug", "ui"], (
        "create_issue must receive plain label name strings; "
        f"got {captured.get('labels')!r}"
    )


def test_orchestrator_ensures_each_label_before_creating_issue() -> None:
    """Each label is ensured once, and all ensures precede the create."""
    issue = _issue(1)
    issue["labels"] = [{"name": "bug", "color": "ff0000"}, {"name": "ui"}]
    api = _FakeApi(issues=[issue])
    order: list[str] = []
    real_create_issue = api.create_issue

    def _recording_create_issue(title: str, body: str, labels: list[str]) -> int:
        order.append("create_issue")
        return real_create_issue(title, body, labels)

    def _recording_ensure_label(
        name: str, color: str = "", description: str = ""
    ) -> None:
        order.append(f"ensure_label:{name}")
        api.calls.append(("ensure_label", name))

    api.create_issue = _recording_create_issue  # type: ignore[method-assign]
    api.ensure_label = _recording_ensure_label  # type: ignore[attr-defined]

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    orch.run()

    ensure_indices = [
        i for i, event in enumerate(order) if event.startswith("ensure_label")
    ]
    assert len(ensure_indices) == 2, (
        f"ensure_label must be called once per label (2); order={order!r}"
    )
    create_index = order.index("create_issue")
    assert all(i < create_index for i in ensure_indices), (
        "all ensure_label calls must happen before the create_issue call; "
        f"order={order!r}"
    )


def test_orchestrator_uses_default_label_color_when_source_label_lacks_color() -> None:
    """A source label without a color is ensured with ``"ededed"``."""
    issue = _issue(1)
    issue["labels"] = [{"name": "ui"}]
    api = _FakeApi(issues=[issue])
    ensured: list[tuple[str, str, str]] = []

    def _recording_ensure_label(name: str, color: str, description: str = "") -> None:
        ensured.append((name, color, description))
        api.calls.append(("ensure_label", name))

    api.ensure_label = _recording_ensure_label  # type: ignore[attr-defined]

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    orch.run()

    assert len(ensured) == 1, (
        f"ensure_label must be called once for the single label; got {ensured!r}"
    )
    assert ensured[0][1] == "ededed", (
        "a source label lacking a color must be ensured with the default "
        f'"ededed"; got {ensured!r}'
    )


def test_orchestrator_wraps_issue_body_with_attribution_block() -> None:
    """The body passed to ``create_issue`` carries the attribution block."""
    issue = _issue(5, title="attributed")
    issue["body"] = "hello world"
    issue["user"] = {"login": "alice"}
    issue["created_at"] = "2024-05-06T12:34:56Z"
    api = _FakeApi(issues=[issue])
    captured: dict[str, Any] = {}
    real_create_issue = api.create_issue

    def _recording_create_issue(title: str, body: str, labels: list[str]) -> int:
        captured["body"] = body
        return real_create_issue(title, body, labels)

    api.create_issue = _recording_create_issue  # type: ignore[method-assign]

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    orch.run()

    body = captured.get("body", "")
    assert body.startswith("> **Migrated from Codeberg**"), (
        f"migrated body must start with the attribution block; got {body!r}"
    )
    assert "5" in body, (
        f"attribution block must contain the issue index 5; got {body!r}"
    )
    assert "alice" in body, (
        f"attribution block must contain the author handle alice; got {body!r}"
    )
    assert "2024-05-06" in body, (
        "attribution block must contain the date part before T "
        f"(2024-05-06); got {body!r}"
    )


# Comment fidelity: fetch, attribution, skips (append-only)


def test_orchestrator_fetches_comments_via_codeberg_client() -> None:
    """Migrated comments come from ``list_comments``, not the payload field.

    The issue payload carries a stale dummy comment while the Codeberg
    seam is configured with the real comment. The orchestrator must
    consult ``list_comments`` with the source issue number and post
    the fetched body.
    """
    real_comment: dict[str, Any] = {
        "type": "Comment",
        "user": {"username": "alice"},
        "created_at": "2024-01-02T10:30:00Z",
        "body": "hello there",
    }
    issue = _issue(1, comments=1)
    api = _FakeApi(issues=[issue], comments_by_issue={1: [real_comment]})
    posted: list[str] = []
    real_create_comment = api.create_comment

    def _recording_create_comment(github_number: int, body: str) -> int:
        posted.append(body)
        return real_create_comment(github_number, body)

    api.create_comment = _recording_create_comment  # type: ignore[method-assign]

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    orch.run()

    assert ("list_comments", "1") in api.calls, (
        "orchestrator must fetch comments via list_comments(1); "
        f"recorded calls: {api.calls!r}"
    )
    assert any("hello there" in body for body in posted), (
        "posted comment body must come from the list_comments return; "
        f"posted={posted!r}"
    )
    assert not any("comment 0 for 1" in body for body in posted), (
        "stale payload comments must not be posted; "
        f"posted={posted!r}"
    )


def test_orchestrator_wraps_comment_bodies_with_attribution() -> None:
    """A fetched comment is posted wrapped with the attribution block."""
    real_comment: dict[str, Any] = {
        "type": "Comment",
        "user": {"username": "alice"},
        "created_at": "2024-01-02T10:30:00Z",
        "body": "hello there",
    }
    api = _FakeApi(issues=[_issue(1)], comments_by_issue={1: [real_comment]})
    captured: dict[str, Any] = {}
    real_create_comment = api.create_comment

    def _recording_create_comment(github_number: int, body: str) -> int:
        captured["body"] = body
        return real_create_comment(github_number, body)

    api.create_comment = _recording_create_comment  # type: ignore[method-assign]

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    orch.run()

    expected = format_comment_body("alice", "2024-01-02", "hello there")
    assert captured.get("body") == expected, (
        "body passed to create_comment must equal the format_comment_body "
        f"output; got {captured.get('body')!r}, expected {expected!r}"
    )


def test_orchestrator_counts_skipped_malformed_comments() -> None:
    """Two well-formed comments plus one malformed (empty body) comment."""
    good_one: dict[str, Any] = {
        "type": "Comment",
        "user": {"username": "alice"},
        "created_at": "2024-01-02T10:30:00Z",
        "body": "hello there",
    }
    good_two: dict[str, Any] = {
        "type": "Comment",
        "user": {"username": "bob"},
        "created_at": "2024-01-03T10:30:00Z",
        "body": "second comment",
    }
    malformed: dict[str, Any] = {
        "type": "Comment",
        "user": {"username": "carol"},
        "created_at": "2024-01-04T10:30:00Z",
        "body": "",
    }
    issue = _issue(1, comments=2)
    api = _FakeApi(
        issues=[issue], comments_by_issue={1: [good_one, good_two, malformed]}
    )

    orch, _fakes = _build(api=api, state=_FakeState(), report=_FakeReport())

    result = orch.run()

    assert result.comments_attempted == 3, (
        "malformed comments count as attempted; "
        f"got {result.comments_attempted!r}"
    )
    assert result.comments_succeeded == 2, (
        "only the two well-formed comments succeed; "
        f"got {result.comments_succeeded!r}"
    )


def test_orchestrator_comment_fetch_failure_fails_issue() -> None:
    """A ``list_comments`` failure fails the issue without checkpointing it."""
    api = _FakeApi(issues=[_issue(1)])
    api.fail_on_list_comments(1)
    state = _FakeState()
    report = _FakeReport()

    orch, _fakes = _build(api=api, state=state, report=report)

    result = orch.run()

    assert any(f.step == "fetch_comments" for f in result.failures), (
        "a comment fetch failure must record a failure with "
        f'step "fetch_comments"; got {result.failures!r}'
    )
    assert any(
        event[0] == "issue_failed" and event[2] == "comment"
        for event in report.events
        if len(event) >= 3
    ), (
        'reporter issue_failed event must carry kind "comment"; '
        f"events={report.events!r}"
    )
    assert result.issues_failed == 1, (
        f"the failed fetch must count one failed issue; got {result.issues_failed!r}"
    )
    assert not any(
        event[0] == "issue_succeeded" and event[1] == "1"
        for event in report.events
    ), (
        "no issue_succeeded may be reported for the fetch-failed issue; "
        f"events={report.events!r}"
    )
    assert not any(
        event[0] == "issue" and event[1] == 1 for event in state.events
    ), (
        "the fetch-failed issue must not be checkpointed; "
        f"events={state.events!r}"
    )
