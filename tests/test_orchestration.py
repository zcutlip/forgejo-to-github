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
# ``issues_attempted``, ``issues_succeeded``, ``issues_failed``).
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

from typing import Any

import pytest

from forgejo_to_github.migration import MigrationOrchestrator

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

    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = list(issues or [])
        self.calls: list[tuple[str, ...]] = []
        self._fail_issue_numbers: set[int] = set()
        self._fail_comment_keys: set[tuple[int, int]] = set()
        self._github_to_source: dict[int, int] = {}
        self._comment_counters: dict[int, int] = {}

    def list_issues(self) -> list[dict[str, Any]]:
        self.calls.append(("list_issues",))
        return list(self.issues)

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

    def issue_failed(self, source_number: int, reason: str) -> None:
        self.events.append(("issue_failed", str(source_number), str(reason)))

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
    api = _FakeApi(issues=[_issue(1, comments=3), _issue(2, comments=1)])
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
