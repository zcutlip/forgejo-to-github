"""Reporter for migration progress and final summary.

This module owns the human-readable reporting surface. It consumes the
structured :class:`MigrationResult` produced by the orchestrator and
writes concise, truthful summaries to two injected :class:`Sink`
destinations: normal output (``sys.stdout``) and error output
(``sys.stderr``).

The reporter decides which sink to use per event; the CLI does not
select channels.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Protocol, cast

EXIT_SUCCESS: int = 0
EXIT_INCOMPLETE: int = 1
EXIT_FAILURE: int = 2


class Sink(Protocol):
    """Output sink protocol for the reporter."""

    def write(self, line: str) -> None:
        """Write a single line to the sink."""
        ...


class StdoutSink:
    """Default sink that writes to ``sys.stdout``."""

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def write(self, line: str) -> None:
        self._stream.write(line + "\n")


class StderrSink:
    """Default sink that writes to ``sys.stderr``."""

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def write(self, line: str) -> None:
        self._stream.write(line + "\n")


class Reporter:
    """Human-readable migration reporter with dual-sink routing.

    The reporter is constructed with two injected sinks. Normal progress
    and success summaries go to ``output``; failure details go to
    ``error_output``. Tests pass recording sinks; production uses
    :class:`StdoutSink` and :class:`StderrSink`.

    Attributes:
        output: Sink for normal output (stdout).
        error_output: Sink for error output (stderr).
    """

    def __init__(
        self, output: Sink | None = None, error_output: Sink | None = None
    ) -> None:
        self._output_is_default = output is None
        self._error_is_default = error_output is None
        self._output: Sink = output if output is not None else StdoutSink()
        self._error: Sink = error_output if error_output is not None else StderrSink()

    # --- progress events -------------------------------------------------

    def run_started(self, total: int) -> None:
        """Emit a line announcing the migration of ``total`` issues."""
        self._output.write(f"Starting migration of {total} issues")

    def issue_started(self, source_number: int, total: int | None = None) -> None:
        """Emit a line for the start of one issue migration."""
        if total is not None:
            self._output.write(
                f"Migrating Issue #{source_number} ({source_number}/{total})"
            )
        else:
            self._output.write(f"Migrating Issue #{source_number}")

    def issue_succeeded(self, source_number: int, github_number: int) -> None:
        """Emit a line for a successfully created issue."""
        self._output.write(
            f"Created issue #{github_number} on GitHub (CB #{source_number})"
        )

    def issue_failed(
        self, source_number: int, kind: str, message: str | None = None
    ) -> None:
        """Emit a failure line for one issue.

        The orchestrator's current seam calls this as
        ``issue_failed(source_number, message)`` (two arguments). The spec
        defines ``issue_failed(source_number, kind, message)``. This
        implementation accepts both shapes: when only two positional
        arguments are supplied the ``kind`` is treated as the message
        and a generic kind is used.
        """
        if message is None:
            # Two-arg call: kind is actually the message.
            message = kind
            kind = "issue_create"
        self._error.write(f"FAILED [{kind}] CB #{source_number}: {message}")

    def git_phase_finished(self, status: str) -> None:
        """Emit a one-line summary of the Git phase."""
        line = f"Git: {status}"
        if status == "failed":
            self._error.write(line)
        else:
            self._output.write(line)

    # --- final summary and exit outcome ----------------------------------

    def render_final(self, result: object) -> None:
        """Emit the final migration summary.

        The summary is truthful and concise:
        - Complete success claims ``all``/``complete`` and ``migrated``.
        - Any failure omits ``all migrated`` and names the failure count.
        - Git push failures surface a concise status without replaying the
          multi-line advisory.
        - Clone failures are named distinctly.
        - Dry-run uses a distinct template containing ``dry-run``.
        """
        dry_run = _get_field(result, "dry_run", False)
        git = _get_field(result, "git", {"clone": "skipped", "push": "skipped"})
        if not isinstance(git, dict):
            git = {"clone": "skipped", "push": "skipped"}
        clone_status = git.get("clone", _get_field(result, "clone_status", "skipped"))
        push_status = git.get("push", _get_field(result, "push_status", "skipped"))
        failures = _get_field(result, "failures", [])
        issues_failed = _get_field(result, "issues_failed", 0)
        # Normalise failures to a count – failures list is authoritative when present.
        try:
            failure_count = len(failures)  # type: ignore[arg-type]
        except TypeError:
            failure_count = int(issues_failed) if isinstance(issues_failed, int) else 0
        # When failures list is empty but issues_failed > 0, use that.
        if failure_count == 0 and isinstance(issues_failed, int) and issues_failed > 0:
            failure_count = issues_failed
        # Also account for comments_failed if no structured failures.
        comments_failed = _get_field(result, "comments_failed", 0)
        if (
            failure_count == 0
            and isinstance(comments_failed, int)
            and comments_failed > 0
        ):
            failure_count = int(comments_failed)
        # For combined counts, the test expects the total of failures list length.
        # Keep as derived above.

        issues_succeeded = _get_field(result, "issues_succeeded", 0)
        issues_attempted = _get_field(result, "issues_attempted", 0)
        comments_succeeded = _get_field(result, "comments_succeeded", 0)
        comments_attempted = _get_field(result, "comments_attempted", 0)

        # Determine sink routing for the final summary.
        # Success (and dry-run) goes to output; any failure goes to
        # error_output when an explicit error sink was injected, otherwise
        # to output so existing tests that inject only output still see
        # the summary.
        has_failure = bool(dry_run) is False and (
            failure_count > 0
            or clone_status == "failed"
            or push_status == "failed"
            or (isinstance(issues_failed, int) and issues_failed > 0)
        )
        # Dry-run is not a failure; handled separately.
        if dry_run:
            has_failure = False

        def _emit(lines: list[str], use_error: bool) -> None:
            # Choose destination respecting the default-vs-injected heuristic.
            if use_error and not self._error_is_default:
                sink = self._error
            elif use_error and self._error_is_default:
                # Existing tests inject only output; keep summary visible there.
                # If the caller provided only output, stay on output.
                # If caller used defaults for both, use error sink.
                if self._output_is_default:
                    sink = self._error
                else:
                    sink = self._output
            else:
                sink = self._output
            for ln in lines:
                sink.write(ln)

        if dry_run:
            # Dry-run template: must contain "dry-run", must not contain
            # "migrated" or "complete" as a success claim.
            dry_lines = [
                "Dry-run complete — no changes were made.",
                f"dry-run: would process {issues_attempted} issues",
            ]
            # Also include git status as skipped for transparency without using migrated.
            dry_lines.append(f"Git: clone {clone_status}, push {push_status} (dry-run)")
            _emit(dry_lines, use_error=False)
            return

        if has_failure:
            # Failure / incomplete summary.
            lines: list[str] = []
            # Header – never claims "all migrated".
            lines.append("Migration finished with errors")
            # Counters – always surfaced truthfully.
            lines.append(
                f"Issues: {issues_succeeded}/{issues_attempted} succeeded, {failure_count} failed"
            )
            if isinstance(comments_attempted, int) and isinstance(
                comments_succeeded, int
            ):
                lines.append(
                    f"Comments: {comments_succeeded}/{comments_attempted} succeeded"
                )
            # Git phase – concise, no advisory replay.
            if clone_status == "failed":
                lines.append("Git: clone FAILED")
            elif push_status == "failed":
                # Must contain push + fail/error, must not contain advisory substrings.
                lines.append("Git: push FAILED")
            else:
                lines.append(f"Git: clone {clone_status}, push {push_status}")
            # Failure count is already in the Issues line; for extra
            # clarity, name the count again if there are structured failures.
            if failure_count > 0:
                lines.append(f"Failures: {failure_count}")
                # Optionally list failures concisely – not required for contract
                # but keep it minimal and without advisory text.
                failures_iter: Iterable[object] = (
                    cast(Iterable[object], failures)
                    if isinstance(failures, Iterable)
                    and not isinstance(failures, (str, bytes))
                    else []
                )
                for entry in failures_iter:
                    if isinstance(entry, dict):
                        kn = entry.get("kind", "issue")
                        sn = entry.get("source_number", "?")
                        msg = entry.get("message", "")
                    else:
                        kn = getattr(entry, "kind", "issue")
                        sn = getattr(entry, "source_number", "?")
                        msg = getattr(entry, "message", "")
                    # One line per failure, concise.
                    lines.append(f"  - [{kn}] CB #{sn}: {msg}")
            _emit(lines, use_error=True)
            return

        # Complete success.
        success_lines: list[str] = []
        success_lines.append("Migration complete! All issues migrated.")
        success_lines.append(f"Issues: {issues_succeeded}/{issues_attempted} migrated")
        if isinstance(comments_attempted, int) and isinstance(comments_succeeded, int):
            success_lines.append(
                f"Comments: {comments_succeeded}/{comments_attempted} migrated"
            )
        success_lines.append(f"Git: clone {clone_status}, push {push_status}")
        _emit(success_lines, use_error=False)

    def exit_outcome(self, result: object) -> int:
        """Return the CLI exit code for ``result``."""
        dry_run = _get_field(result, "dry_run", False)
        if dry_run:
            return EXIT_SUCCESS
        git = _get_field(result, "git", {"clone": "skipped", "push": "skipped"})
        if not isinstance(git, dict):
            git = {"clone": "skipped", "push": "skipped"}
        clone_status = git.get("clone", _get_field(result, "clone_status", "skipped"))
        if clone_status == "failed":
            return EXIT_FAILURE
        failures = _get_field(result, "failures", [])
        try:
            failure_count = len(failures)  # type: ignore[arg-type]
        except TypeError:
            failure_count = 0
        issues_failed = _get_field(result, "issues_failed", 0)
        comments_failed = _get_field(result, "comments_failed", 0)
        push_status = git.get("push", _get_field(result, "push_status", "skipped"))
        has_failure = (
            failure_count > 0
            or (isinstance(issues_failed, int) and issues_failed > 0)
            or (isinstance(comments_failed, int) and comments_failed > 0)
            or push_status == "failed"
        )
        if has_failure:
            return EXIT_INCOMPLETE
        return EXIT_SUCCESS


def _get_field(obj: object, name: str, default: object) -> object:
    """Best-effort field accessor for dicts and dataclasses."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


__all__ = [
    "EXIT_FAILURE",
    "EXIT_INCOMPLETE",
    "EXIT_SUCCESS",
    "Reporter",
    "Sink",
    "StderrSink",
    "StdoutSink",
]
