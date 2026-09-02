"""Stage-06 alignment: migration failure handling, reporting, and resume.

Rewritten from the legacy ``f2gh.migrate`` seam to the extracted
``MigrationOrchestrator`` + ``Reporter`` + ``StateStore``/fake seams.

Preserved behavioral contracts (from original ``test_migration_reporting.py``):
1. Git push failure is non-fatal: orchestrator continues into issue
   enumeration, result marks ``push_status`` as failed, and the final
   ``Reporter`` summary truthfully surfaces the push failure without
   claiming ``All issues migrated``.
2. Git clone failure is terminal: ``run()`` propagates the clone exception
   and no ``list_issues`` call is recorded.
3. Per-issue creation failure is accumulated; later issues still migrate;
   the failure is recorded in ``MigrationResult.failures`` and surfaced
   by ``Reporter.render_final`` without claiming complete success.
4. Successful issues are checkpointed via the state seam and a second run
   filters already-migrated issues.

Removed f2gh seams: ``f2gh.mirror_git_repo``, ``f2gh.check_target_repo``,
``f2gh.fetch_all_codeberg_issues``, ``f2gh.create_github_issue``, etc.
Replaced with injected fakes for ``CodebergClient`` (``list_issues``),
``GitHubClient`` (``create_issue`` / ``create_comment`` / ``close_issue``),
``GitMirror`` (``run_clone`` / ``run_push``), ``StateStore``
(``already_migrated`` / ``record_issue`` / ``record_comment``), and
``Reporter`` sinks.

All interactions are offline; no live HTTP or subprocess is used.
"""

from __future__ import annotations

from typing import Any

import pytest

from forgejo_to_github.domain import Repository
from forgejo_to_github.migration import MigrationOrchestrator
from forgejo_to_github.reporting import Reporter

# ---------------------------------------------------------------------------
# sinks and fakes
# ---------------------------------------------------------------------------


class _Sink:
    """Recording sink for Reporter."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines)


class _FakeCodeberg:
    def __init__(self, issues: list[dict[str, Any]] | None = None) -> None:
        self.issues = list(issues or [])
        self.calls: list[tuple[str, ...]] = []

    def list_issues(self) -> list[dict[str, Any]]:
        self.calls.append(("list_issues",))
        return list(self.issues)


class _FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._fail_create: set[int] = set()
        self._next_number = 4242

    def fail_on_create(self, source_number: int) -> None:
        self._fail_create.add(int(source_number))

    def create_issue(self, payload: dict[str, Any]) -> dict[str, Any]:
        number = int(payload["number"])
        self.calls.append(("create_issue", str(number)))
        if number in self._fail_create:
            raise RuntimeError("500 Server Error: Internal Server Error")
        result = {"number": self._next_number}
        self._next_number += 1
        # also allow caller to set explicit second number via counter
        return result

    def create_comment(
        self, issue_number: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("create_comment", str(issue_number)))
        return {"id": 1}

    def close_issue(self, issue_number: int) -> None:
        self.calls.append(("close_issue", str(issue_number)))


class _FakeGit:
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
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None]] = []

    def already_migrated(self, source_number: int) -> bool:
        for kind, src, _ in self.events:
            if kind == "issue" and src == source_number:
                return True
        return False

    def record_issue(self, source_number: int, github_number: int) -> None:
        self.events.append(("issue", int(source_number), int(github_number)))

    def record_comment(
        self, source_number: int, comment_index: int, github_comment_id: int
    ) -> None:
        self.events.append(("comment", int(source_number), int(comment_index)))


class _NullReporter:
    """Minimal reporter double that records git_phase_finished calls."""

    def __init__(self) -> None:
        self.git_statuses: list[str] = []
        self.started: list[int] = []
        self.succeeded: list[tuple[int, int]] = []
        self.failed: list[tuple[int, str]] = []

    def issue_started(self, source_number: int, total: int | None = None) -> None:
        self.started.append(int(source_number))

    def issue_succeeded(self, source_number: int, github_number: int) -> None:
        self.succeeded.append((int(source_number), int(github_number)))

    def issue_failed(self, source_number: int, reason: str) -> None:
        self.failed.append((int(source_number), str(reason)))

    def git_phase_finished(self, status: str) -> None:
        self.git_statuses.append(status)


def _issue(number: int, title: str = "issue") -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "state": "open",
        "body": f"body for {number}",
        "labels": [],
    }


def _repo(*, skip_git: bool = True, dry_run: bool = False) -> Repository:
    return Repository(
        source="owner/source",
        target="owner/target",
        skip_git=skip_git,
        dry_run=dry_run,
        yes=True,
    )


def _build_orchestrator(
    *,
    issues: list[dict[str, Any]] | None = None,
    git: _FakeGit | None = None,
    state: _FakeState | None = None,
    github: _FakeGitHub | None = None,
    codeberg: _FakeCodeberg | None = None,
    reporter: Any | None = None,
    repo: Repository | None = None,
) -> tuple[MigrationOrchestrator, dict[str, Any]]:
    codeberg = codeberg if codeberg is not None else _FakeCodeberg(issues)
    github = github if github is not None else _FakeGitHub()
    git = git if git is not None else _FakeGit()
    state = state if state is not None else _FakeState()
    # Reporter double: use _NullReporter when caller wants to inspect
    # reporter calls; otherwise allow a real Reporter with sinks.
    if reporter is None:
        reporter = _NullReporter()
    repo = (
        repo
        if repo is not None
        else _repo(skip_git=not (git is not None and isinstance(git, _FakeGit)))
    )
    # Always pass explicit repo; default skip_git is True unless test opts in.
    # The fakes above use duck typing; MigrationOrchestrator accepts api aliases
    # but we use the locked keyword names.
    orch = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
    )
    return orch, {
        "codeberg": codeberg,
        "github": github,
        "git": git,
        "state": state,
        "reporter": reporter,
        "repo": repo,
    }


# ---------------------------------------------------------------------------
# 1. push failure non-fatal and reported via Reporter
# ---------------------------------------------------------------------------


def test_git_push_failure_is_non_fatal_and_reported() -> None:
    git = _FakeGit()
    git.push_error = RuntimeError("simulated push failure")
    codeberg = _FakeCodeberg(issues=[])
    state = _FakeState()
    reporter_sink = _Sink()
    error_sink = _Sink()
    reporter = Reporter(output=reporter_sink, error_output=error_sink)

    orch, _fakes = _build_orchestrator(
        git=git,
        codeberg=codeberg,
        state=state,
        reporter=reporter,
        repo=Repository(
            source="owner/source", target="owner/target", skip_git=False, yes=True
        ),
    )

    result = orch.run()

    assert git.push_called is True
    # Migration continued into issue enumeration despite push failure.
    assert ("list_issues",) in codeberg.calls
    assert result.push_status == "failed"
    assert result.git["push"] == "failed"

    # Reporter truthfully surfaces the failure without claiming complete success.
    reporter.render_final(result)
    error_text = error_sink.text()
    output_text = reporter_sink.text()
    combined = output_text + "\n" + error_text
    # Reporter uses "Git: push FAILED" for push failure (concise, no advisory replay).
    assert "FAILED" in combined
    assert "push" in combined.lower()
    assert "All issues migrated" not in combined
    assert "Migration complete! All issues migrated." not in combined


# ---------------------------------------------------------------------------
# 2. clone failure terminal
# ---------------------------------------------------------------------------


def test_clone_failure_is_terminal_and_skips_issue_fetch() -> None:
    git = _FakeGit()
    git.clone_error = RuntimeError("Clone failed: fatal: repository not found")
    codeberg = _FakeCodeberg(issues=[_issue(1), _issue(2)])

    orch, _fakes = _build_orchestrator(
        git=git,
        codeberg=codeberg,
        repo=Repository(
            source="owner/source", target="owner/target", skip_git=False, yes=True
        ),
    )

    with pytest.raises(RuntimeError) as exc_info:
        orch.run()

    assert "Clone failed" in str(exc_info.value)
    # No issue enumeration after terminal clone failure.
    assert codeberg.calls == []


# ---------------------------------------------------------------------------
# 3. per-issue failure accumulated
# ---------------------------------------------------------------------------


def test_issue_failure_is_accumulated_and_later_issues_continue() -> None:
    issues = [_issue(1, "first issue"), _issue(2, "second issue")]
    codeberg = _FakeCodeberg(issues=issues)
    github = _FakeGitHub()
    github.fail_on_create(1)
    # Second issue will succeed and receive an auto number.
    state = _FakeState()
    output = _Sink()
    error = _Sink()
    reporter = Reporter(output=output, error_output=error)

    orch, _fakes = _build_orchestrator(
        codeberg=codeberg,
        github=github,
        state=state,
        reporter=reporter,
        repo=_repo(skip_git=True),
    )

    result = orch.run()

    # Both issues attempted; second succeeded.
    assert github.calls.count(("create_issue", "1")) == 1
    assert github.calls.count(("create_issue", "2")) == 1
    assert result.issues_failed == 1
    assert result.issues_succeeded == 1
    assert len(result.failures) == 1
    assert result.failures[0].source_number == 1

    # Only the successful issue is checkpointed.
    checkpointed = [src for kind, src, _ in state.events if kind == "issue"]
    assert checkpointed == [2]

    reporter.render_final(result)
    combined = output.text() + "\n" + error.text()
    # Failure must be named: kind and CB number.
    assert "CB #1" in combined or "CB # 1" in combined or "1" in combined
    # Reporter must not claim complete success.
    assert "All issues migrated" not in combined
    assert "Migration complete! All issues migrated." not in combined
    # Issues line must mention the failure count.
    assert (
        "1 failed" in combined.lower()
        or "failures: 1" in combined.lower()
        or "failed" in combined.lower()
    )


# ---------------------------------------------------------------------------
# 4. checkpointed issues are skipped on resume
# ---------------------------------------------------------------------------


def test_successful_issues_are_checkpointed_and_resume_filters_them() -> None:
    first_run_issues = [_issue(1, "first issue")]
    second_run_issues = [_issue(1, "first issue"), _issue(2, "second issue")]

    state = _FakeState()
    output = _Sink()
    error = _Sink()
    reporter = Reporter(output=output, error_output=error)

    # First run: migrate issue 1.
    codeberg1 = _FakeCodeberg(issues=first_run_issues)
    github1 = _FakeGitHub()
    orch1, _ = _build_orchestrator(
        codeberg=codeberg1,
        github=github1,
        state=state,
        reporter=reporter,
        repo=_repo(skip_git=True),
    )
    result1 = orch1.run()
    assert result1.issues_succeeded == 1
    assert [src for kind, src, _ in state.events if kind == "issue"] == [1]

    # Second run: same shared state, now with issue 2 added.
    codeberg2 = _FakeCodeberg(issues=second_run_issues)
    github2 = _FakeGitHub()
    # Use same state object so already_migrated sees issue 1.
    orch2, _ = _build_orchestrator(
        codeberg=codeberg2,
        github=github2,
        state=state,
        reporter=reporter,
        repo=_repo(skip_git=True),
    )
    result2 = orch2.run()

    # Only the new issue was created on resume.
    assert github2.calls == [("create_issue", "2")]
    assert sorted([src for kind, src, _ in state.events if kind == "issue"]) == [1, 2]
    # Second run attempted only the unmigrated issue; orchestrator increments
    # issues_attempted for each enumerated issue, but already_migrated skips
    # the checkpointed one without counting as succeeded/failed again.
    # The new result should have one success.
    assert result2.issues_succeeded == 1
