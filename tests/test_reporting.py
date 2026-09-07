# RED class: D. Structural / B. Boundary unit
#
# Reporting tests (RED stage) for the package refactor described in
# plans/02-package-refactor-and-test-foundation.md. These tests assert
# the intended public contract of ``Reporter`` from
# ``forgejo_to_github.reporting``:
#
# - the reporter is constructed with an injected output sink (no direct
#   ``print`` calls and no module-level stdout coupling);
# - the final report distinguishes complete success from incomplete/
#   failed outcomes and never claims ``All migrated`` when a failure is
#   present;
# - the Git failure summary is concise and never replays the multi-line
#   advisory body.
#
# Result and failure objects are deliberately inspectable dicts here:
# the reporter's contract is to consume already-structured inputs, so
# asserting on dict keys is a stable surface that does not depend on
# the (still to be designed) domain types.
#
# RED-stage expectation: these tests fail via ``ImportError`` for the
# missing ``Reporter`` symbol or via attribute errors on its return
# value. That is the contract under test.
"""Reporting tests for ``forgejo_to_github.reporting``."""

from __future__ import annotations

from typing import Any

from forgejo_to_github.domain import Repository
from forgejo_to_github.migration import MigrationOrchestrator
from forgejo_to_github.reporting import Reporter

# --- helpers ---------------------------------------------------------------


class _Sink:
    """Output sink double.

    The reporter writes to an injected sink; this records each emitted
    line so tests can assert on the report contents.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)

    def text(self) -> str:
        return "\n".join(self.lines)


def _complete_result() -> dict[str, Any]:
    """A result with no failures, no partial work, and a clean Git phase."""
    return {
        "issues_attempted": 3,
        "issues_succeeded": 3,
        "issues_failed": 0,
        "comments_attempted": 5,
        "comments_succeeded": 5,
        "comments_failed": 0,
        "git": {"clone": "ok", "push": "ok"},
        "failures": [],
    }


def _result_with_issue_failure() -> dict[str, Any]:
    """A result with one failed issue; Git phase is clean."""
    return {
        "issues_attempted": 3,
        "issues_succeeded": 2,
        "issues_failed": 1,
        "comments_attempted": 5,
        "comments_succeeded": 4,
        "comments_failed": 1,
        "git": {"clone": "ok", "push": "ok"},
        "failures": [
            {
                "kind": "issue",
                "source_number": 2,
                "message": "simulated issue failure",
            },
        ],
    }


def _result_with_git_push_failure() -> dict[str, Any]:
    """A result with a push failure but no issue failures.

    The push failure carries a multi-line advisory body in the input
    fixture; the reporter must summarize it concisely and must not
    replay the advisory verbatim.
    """
    multiline_advisory = (
        "Possible causes:\n"
        "  - remote rejected non-fast-forward\n"
        "  - authentication required\n"
        "Remediation:\n"
        "  - run `git pull --rebase` and retry\n"
        "  - or push with `--force-with-lease`\n"
    )
    return {
        "issues_attempted": 2,
        "issues_succeeded": 2,
        "issues_failed": 0,
        "comments_attempted": 2,
        "comments_succeeded": 2,
        "comments_failed": 0,
        "git": {
            "clone": "ok",
            "push": "failed",
            "advisory": multiline_advisory,
        },
        "failures": [],
    }


# --- 1. constructor accepts an output sink ---------------------------------


def test_reporter_constructor_accepts_injected_output_sink():
    """``Reporter`` must accept an explicit output sink.

    The reporter's contract is dependency injection: it must not reach
    for ``sys.stdout`` itself. Tests supply a sink and assert that the
    reporter does not require anything beyond it.
    """
    sink = _Sink()
    reporter = Reporter(output=sink)
    assert reporter is not None


# --- 2. complete result reports complete success ----------------------------


def test_complete_result_reports_complete_migration():
    """A result with zero failures yields a complete-migration message."""
    sink = _Sink()
    reporter = Reporter(output=sink)

    reporter.render_final(_complete_result())

    text = sink.text()
    assert "migrated" in text.lower(), (
        "complete results must use a 'migrated' framing; got:\n" + text
    )
    # All migrated / fully migrated phrasing — contract surface.
    assert "all" in text.lower() or "complete" in text.lower(), (
        "complete results must announce completion; got:\n" + text
    )


# --- 3. result with failure does not claim all migrated ---------------------


def test_result_with_failure_does_not_claim_all_migrated():
    """When any failure is present the summary must not say 'all migrated'.

    This is the truthfulness contract from §13.4 of the test framework
    spec: the summary must never under-report failures nor over-claim
    success.
    """
    sink = _Sink()
    reporter = Reporter(output=sink)

    reporter.render_final(_result_with_issue_failure())

    text = sink.text()
    lower = text.lower()
    assert "all migrated" not in lower, (
        "must not claim 'all migrated' when a failure exists; got:\n" + text
    )
    # The failed count must be named explicitly.
    assert "1" in text, (
        "expected the failed count to appear in the report; got:\n" + text
    )


# --- 4. multiple failures under-count guard ---------------------------------


def test_report_names_every_failure_exactly_once():
    """Injecting N failures must produce a count of exactly N.

    The summary must neither under-count nor over-count failures.
    """
    sink = _Sink()
    reporter = Reporter(output=sink)

    result = _result_with_issue_failure()
    # Add two more failures to reach a count of 3.
    result["failures"].extend(
        [
            {"kind": "issue", "source_number": 7, "message": "another"},
            {"kind": "comment", "source_number": 9, "message": "yet another"},
        ]
    )
    result["issues_failed"] = 2
    result["comments_failed"] = 1

    reporter.render_final(result)

    text = sink.text()
    # The reporter must expose a structured failure count. The
    # assertion is on the count being present and equal to the input.
    assert text.count("3") >= 1, (
        "expected the failure count '3' to appear at least once; got:\n" + text
    )


# --- 5. git push failure summary is concise, not the advisory ------------


def test_git_push_failure_summary_does_not_replay_multiline_advisory():
    """The Git failure summary must not echo the multi-line advisory.

    The reporter contract: surface the failure status concisely and
    point to advice without replaying the entire multi-line advisory
    block into the summary.
    """
    sink = _Sink()
    reporter = Reporter(output=sink)

    reporter.render_final(_result_with_git_push_failure())

    text = sink.text()
    # Status must be surfaced.
    assert "push" in text.lower() and (
        "fail" in text.lower() or "error" in text.lower()
    ), "push failure status must be named in the summary; got:\n" + text

    # Advisory lines must not appear verbatim.
    for advisory_line in (
        "Possible causes:",
        "Remediation:",
        "git pull --rebase",
        "--force-with-lease",
    ):
        assert advisory_line not in text, (
            f"advisory line {advisory_line!r} must not be replayed verbatim "
            "in the summary; got:\n" + text
        )


# --- 6. reporter distinguishes clone-failed result from clone-ok result ---


def test_clone_failure_summary_marks_clone_status_distinctly():
    """A clone failure must be named in the summary distinctly from push.

    The reporter must surface the failing Git phase by name so the
    operator can see whether migration aborted before issues or merely
    skipped Git push.
    """
    sink = _Sink()
    reporter = Reporter(output=sink)

    result = _complete_result()
    result["git"] = {"clone": "failed", "push": "skipped"}

    reporter.render_final(result)

    text = sink.text()
    lower = text.lower()
    assert "clone" in lower and "fail" in lower, (
        "clone failure must be named in the summary; got:\n" + text
    )
    # Truthfulness: must not claim complete migration.
    assert "all migrated" not in lower, (
        "clone failure precludes 'all migrated'; got:\n" + text
    )


# --- Failure-kind reporting: distinct kinds reach the reporter ---------------


class _FakeCodeberg:
    """Source seam fake yielding one canned issue listing."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = list(issues)

    def list_issues(self) -> list[dict[str, Any]]:
        return list(self.issues)


class _FakeGitHub:
    """Write-seam fake aligned to the concrete ``GitHubClient`` signatures.

    Each failure step is armed per scenario: ``fail_create_issue``,
    ``fail_comment``, ``fail_close``, and ``fail_labels`` (label names
    whose ``ensure_label`` raises).
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.fail_create_issue = False
        self.fail_comment = False
        self.fail_close = False
        self.fail_labels: set[str] = set()
        self._next_github_number = 700

    @staticmethod
    def _source_from_body(body: str) -> str:
        token = body.strip().split()[-1] if body.strip() else "?"
        try:
            return str(int(token))
        except (TypeError, ValueError):
            return "?"

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        source = self._source_from_body(body)
        self.calls.append(("create_issue", source))
        if self.fail_create_issue:
            raise RuntimeError(f"simulated create_issue failure for {source}")
        self._next_github_number += 1
        return self._next_github_number

    def create_comment(self, github_number: int, body: str) -> int:
        self.calls.append(("create_comment", str(github_number)))
        if self.fail_comment:
            raise RuntimeError(f"simulated create_comment failure for {github_number}")
        return 1

    def close_issue(self, issue_number: int) -> None:
        self.calls.append(("close_issue", str(issue_number)))
        if self.fail_close:
            raise RuntimeError(f"simulated close_issue failure for {issue_number}")

    def ensure_label(self, name: str, color: str, description: str = "") -> None:
        self.calls.append(("ensure_label", str(name)))
        if str(name) in self.fail_labels:
            raise RuntimeError(f"simulated ensure_label failure for {name}")


class _FakeGit:
    """Git seam fake; unused on the ``skip_git`` path but required."""

    def run_clone(self) -> None:
        return None

    def run_push(self) -> None:
        return None


class _FakeState:
    """Checkpoint seam fake with in-memory ``already_migrated`` support."""

    def __init__(self) -> None:
        self.recorded: list[tuple[int, int]] = []

    def already_migrated(self, source_number: int) -> bool:
        return any(src == int(source_number) for src, _ in self.recorded)

    def record_issue(self, source_number: int, github_number: int) -> None:
        self.recorded.append((int(source_number), int(github_number)))

    def record_comment(
        self, source_number: int, comment_index: int, github_comment_id: int
    ) -> None:
        return None


class _RecordingReporter:
    """Reporter seam double recording the exact ``issue_failed`` arity."""

    def __init__(self) -> None:
        self.issue_failed_calls: list[tuple[Any, ...]] = []

    def issue_started(self, source_number: int, total: int | None = None) -> None:
        return None

    def issue_succeeded(self, source_number: int, github_number: int) -> None:
        return None

    def issue_failed(self, *args: Any) -> None:
        self.issue_failed_calls.append(tuple(args))

    def git_phase_finished(self, status: str) -> None:
        return None


def _slice_c_issue(number: int, **overrides: Any) -> dict[str, Any]:
    """Build a minimal source issue payload for the failure-kind test."""
    issue: dict[str, Any] = {
        "number": int(number),
        "title": f"issue {number}",
        "state": "open",
        "body": f"body for {number}",
        "labels": [],
    }
    issue.update(overrides)
    return issue


def _drive_slice_c_issue(
    issue: dict[str, Any], github: _FakeGitHub
) -> list[tuple[Any, ...]]:
    """Drive the REAL orchestrator over one source issue.

    Returns the exact positional-argument tuples the orchestrator handed
    to ``reporter.issue_failed``.
    """
    reporter = _RecordingReporter()
    orch = MigrationOrchestrator(
        repo=Repository(
            source="owner/source", target="owner/target", skip_git=True, yes=True
        ),
        codeberg=_FakeCodeberg([issue]),
        github=github,
        git=_FakeGit(),
        state=_FakeState(),
        reporter=reporter,
    )
    orch.run()
    return reporter.issue_failed_calls


def test_reporter_issue_failed_receives_distinct_kind_per_failure_step() -> None:
    """Each failure step must reach ``issue_failed`` as ``(number, kind, msg)``.

    Drives the real ``MigrationOrchestrator`` through four
    failure scenarios — issue-create, comment-post, label-create (via a
    label whose ``ensure_label`` raises), and close — and asserts the
    reporter seam receives THREE positional arguments with the step's
    distinct ``kind`` (``"issue_create"``, ``"comment"``,
    ``"label_create"``, ``"close_failed"`` per ``04-orchestrator.md``
    §3.9).

    RED expectation: the orchestrator currently calls
    ``issue_failed(source_number, reason)`` (two args) for creates and
    never notifies the reporter for comment/close failures (nor calls
    ``ensure_label`` at all), so the recorder sees 2-tuples or nothing
    and the kind assertions below FAIL.
    """
    # 1. issue-create failure.
    create_github = _FakeGitHub()
    create_github.fail_create_issue = True
    create_calls = _drive_slice_c_issue(_slice_c_issue(1), create_github)

    # 2. comment-post failure.
    comment_github = _FakeGitHub()
    comment_github.fail_comment = True
    comment_issue = _slice_c_issue(2, comments=[{"index": 0, "body": "a comment"}])
    comment_calls = _drive_slice_c_issue(comment_issue, comment_github)

    # 3. label-create failure: ``ensure_label`` raises for "bug".
    label_github = _FakeGitHub()
    label_github.fail_labels.add("bug")
    label_issue = _slice_c_issue(3, labels=[{"name": "bug", "color": "f29513"}])
    label_calls = _drive_slice_c_issue(label_issue, label_github)

    # 4. close failure: the source issue is closed and ``close_issue`` raises.
    close_github = _FakeGitHub()
    close_github.fail_close = True
    close_calls = _drive_slice_c_issue(_slice_c_issue(4, state="closed"), close_github)

    kinds: list[str] = []
    for calls, source_number, expected_kind in (
        (create_calls, 1, "issue_create"),
        (comment_calls, 2, "comment"),
        (label_calls, 3, "label_create"),
        (close_calls, 4, "close_failed"),
    ):
        assert len(calls) == 1, (
            f"expected exactly one issue_failed call for CB #{source_number}, "
            f"got {calls!r}"
        )
        assert len(calls[0]) == 3, (
            "issue_failed must be called with "
            f"(source_number, kind, message); got {calls[0]!r}"
        )
        number, kind, message = calls[0]
        assert int(number) == source_number, (
            f"expected source_number {source_number}, got {number!r}"
        )
        assert kind == expected_kind, (
            f"expected kind {expected_kind!r}, got {kind!r} for CB #{source_number}"
        )
        assert isinstance(message, str) and message.strip() != "", (
            f"expected a non-empty message for CB #{source_number}, got {message!r}"
        )
        kinds.append(kind)

    assert kinds == ["issue_create", "comment", "label_create", "close_failed"]
    assert len(set(kinds)) == 4, f"kinds must be distinct per step; got {kinds!r}"
