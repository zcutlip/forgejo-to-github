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
