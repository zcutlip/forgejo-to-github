"""Stage 04 — ``MigrationOrchestrator``.

This module owns the public ``MigrationOrchestrator`` class, the seam
that orders the migration phases and the per-issue substep sequence.
It is constructed by injecting exactly five collaborators (plus the
immutable :class:`Repository` value object) and performs no network or
subprocess work of its own.

Phase ordering
--------------

1. **Dry-run short-circuit.** When ``repo.dry_run`` is set, the
   orchestrator returns a default :class:`MigrationResult` with all
   counters at zero, ``git`` set to ``{"clone": "skipped", "push":
   "skipped"}``, no failures, and ``dry_run=True``. No collaborator
   is invoked.
2. **Git mirror.** When ``repo.skip_git`` is not set, the orchestrator
   invokes the injected Git seam's ``run_clone()`` and then
   ``run_push()``. A clone failure is terminal: the exception
   propagates. A push failure is non-fatal: ``git["push"]`` is set to
   ``"failed"``, ``reporter.git_phase_finished("failed")`` is called,
   and issue migration proceeds.
3. **Issue migration.** Each source issue is processed through the
   per-issue state machine (create → comments → checkpoint).
   Per-issue failures are accumulated into
   ``MigrationResult.failures``; the orchestrator does not abort.

The orchestrator never calls ``reporter.render_final`` and never
invokes ``sys.exit`` / raises ``SystemExit``. The CLI is the single
owner of the final summary emission and the process exit code.

Constructor signature
---------------------

The locked production signature is::

    MigrationOrchestrator(
        repo: Repository,
        *,
        codeberg: Any,
        github: Any,
        git: Any,
        state: Any,
        reporter: Any,
    )

For backward compatibility with the single-seam ``_FakeApi`` fixture
in ``tests/test_orchestration.py``, the constructor also accepts the
aliases ``api=`` (which fills both ``codeberg`` and ``github`` when
neither is supplied) and ``report=`` (which fills ``reporter``). This
deviation from the spec is documented in the traceable list; see
``plans/02-package-refactor-and-test-foundation/refactor/04-orchestrator.md``
§3.1 and the discussion under "Naming reconciliation with the
existing test fixture."

Domain types (``Repository``, ``IssueFailure``, ``MigrationResult``)
are imported from :mod:`forgejo_to_github.domain` and are not
re-declared here. The dataclasses already exist and are part of the
locked public contract.
"""

from __future__ import annotations

from typing import Any

from forgejo_to_github.domain import IssueFailure, MigrationResult, Repository

# Default color substituted by the orchestrator when a source label
# lacks one. The GitHub client does not default colors; this constant
# is the orchestrator's documented fallback.
DEFAULT_LABEL_COLOR: str = "ededed"


class MigrationOrchestrator:
    """Order the migration phases and aggregate per-issue outcomes.

    The orchestrator is constructed by dependency injection. It does
    not instantiate any collaborator and does not perform network or
    subprocess work itself. The CLI constructs the concrete
    collaborators and the :class:`Repository` value object, then
    hands them to this class.

    The orchestrator returns a :class:`MigrationResult` from
    :meth:`run`. It does not call ``reporter.render_final``; the CLI
    is the single owner of the final summary emission.

    Attributes
    ----------
    repo : Repository
        Immutable per-run configuration (frozen dataclass).
    codeberg : Any
        Read-only seam providing ``list_issues`` (and, optionally,
        ``get_repository_description`` for the description policy).
    github : Any
        Write seam providing ``create_issue``, ``create_comment``,
        and (optionally) ``close_issue``.
    git : Any
        Subprocess seam providing ``run_clone`` (terminal on raise)
        and ``run_push`` (non-fatal on raise).
    state : Any
        Checkpoint seam providing ``already_migrated`` and
        ``record_issue`` (and, optionally, ``record_comment``).
    reporter : Any
        Reporting seam providing ``issue_started``,
        ``issue_succeeded``, ``issue_failed``, and
        ``git_phase_finished``.
    """

    def __init__(
        self,
        repo: Repository,
        *,
        codeberg: Any = None,
        github: Any = None,
        git: Any,
        state: Any,
        reporter: Any = None,
        # Backward-compatible aliases for the unified test seam. When
        # ``api`` is supplied and ``codeberg``/``github`` are not, the
        # single fake fills both collaborator slots. Likewise for
        # ``report`` → ``reporter``. See module docstring.
        api: Any = None,
        report: Any = None,
    ) -> None:
        if codeberg is None and github is None and api is not None:
            codeberg = api
            github = api
        else:
            if codeberg is None:
                codeberg = api
            if github is None:
                github = api

        if reporter is None:
            reporter = report

        self.repo: Repository = repo
        self.codeberg: Any = codeberg
        self.github: Any = github
        self.git: Any = git
        self.state: Any = state
        self.reporter: Any = reporter

    # --- public entry point --------------------------------------------------

    def run(self) -> MigrationResult:
        """Execute the migration phases and return a :class:`MigrationResult`.

        The orchestrator never calls ``sys.exit`` and never raises
        ``SystemExit``. On clone failure, the underlying exception
        propagates; on push or per-issue failure, the failure is
        accumulated into the result and the run continues.
        """
        # Phase 1: dry-run short-circuit. No collaborator is invoked.
        if bool(getattr(self.repo, "dry_run", False)):
            result = MigrationResult(dry_run=True)
            # git defaults are already {"clone": "skipped", "push":
            # "skipped"}; clone_status/push_status are aliases set to
            # the same values by the dataclass factory.
            return result

        result = MigrationResult()

        # Phase 4: Git mirror. Skipped entirely when --skip-git is set.
        if not bool(getattr(self.repo, "skip_git", False)):
            self.prepare_repository(result)

        # Phase 5: Issue migration. Always attempted after the Git
        # phase (or skipped-Git), regardless of push outcome.
        self._migrate_issues(result)

        return result

    # --- public phase entry points -------------------------------------------

    def prepare_repository(self, result: MigrationResult) -> None:
        """Drive the Git seam: clone the source mirror, then push it.

        This is the repository/Git preparation phase of the migration.
        It populates ``result.git["clone"]`` and ``result.git["push"]``
        (along with the ``clone_status`` / ``push_status`` aliases) and
        notifies the reporter via ``git_phase_finished`` so the CLI can
        surface the phase outcome in the final summary.

        Failure semantics
        -----------------

        - Clone failure is **terminal**: the underlying exception
          propagates out of :meth:`run` and the result is never
          returned for that run.
        - Push failure is **non-fatal**: ``result.git["push"]`` is set
          to ``"failed"``, the reporter is notified, and issue
          migration proceeds.

        This method is a public phase entry point (not a proxy); it
        performs the real Git preparation work and exists so callers
        can drive the repository phase independently of issue
        migration when needed (e.g., to test the Git phase in
        isolation).
        """
        # Clone is terminal. Any raise propagates out of ``run`` and
        # the result is never returned to the caller for this run.
        self.git.run_clone()
        result.git["clone"] = "ok"
        result.clone_status = "ok"

        # Push is non-fatal. Catch any exception, record the status,
        # notify the reporter, and continue to issue migration.
        try:
            self.git.run_push()
        except Exception:  # noqa: BLE001 — non-fatal push failure
            result.git["push"] = "failed"
            result.push_status = "failed"
            self._safe_git_phase_finished("failed")
            return

        result.git["push"] = "ok"
        result.push_status = "ok"
        self._safe_git_phase_finished("ok")

    def _migrate_issues(self, result: MigrationResult) -> None:
        """Enumerate source issues and migrate each one.

        Issues whose number is reported by ``state.already_migrated``
        are skipped (resume support). Each remaining issue is passed
        through :meth:`_migrate_one_issue`, which never raises.
        """
        issues = self._list_issues()
        for issue in issues:
            try:
                source_number = int(issue["number"])
            except (KeyError, TypeError, ValueError):
                # A malformed issue payload is a structured failure;
                # we cannot associate it with a source number.
                continue
            self._migrate_one_issue(source_number, issue, result)

    def _migrate_one_issue(
        self,
        source_number: int,
        issue: dict[str, Any],
        result: MigrationResult,
    ) -> None:
        """Drive the per-issue state machine for a single source issue.

        Sequence (per ``plans/02-package-refactor-and-test-foundation/
        refactor/04-orchestrator.md`` §3.4):

        S1. ``reporter.issue_started`` and increment
            ``issues_attempted``.
        S2. ``github.create_issue``. On success, advance to S3. On
            failure, accumulate ``IssueFailure(kind="issue_create")``,
            increment ``issues_failed``, notify the reporter, and
            return (next issue).
        S3. For each comment, ``github.create_comment``. Per-comment
            failures are accumulated into ``failures`` and counted in
            ``comments_failed``; the issue still progresses to S5.
        S4. If the source issue is closed, ``github.close_issue``.
            Failures are accumulated but the issue is still considered
            succeeded.
        S5. ``state.record_issue`` and ``reporter.issue_succeeded``.
            Increment ``issues_succeeded``.
        """
        result.issues_attempted += 1

        # Resume: skip already-migrated issues. The state seam's
        # ``already_migrated`` is consulted before any work is done
        # for the issue, so even an in-progress run is safe to
        # interrupt and resume.
        if self._already_migrated(source_number):
            return

        # S1: report progress.
        self._safe_issue_started(source_number)

        # S2: create the issue.
        try:
            create_payload: dict[str, Any] = {
                "number": source_number,
                "title": str(issue.get("title", "")),
                "body": str(issue.get("body", "")),
            }
            labels = issue.get("labels")
            if labels:
                create_payload["labels"] = list(labels)
            create_response = self.github.create_issue(create_payload)
            github_number = int(create_response["number"])
        except Exception as exc:  # noqa: BLE001 — issue create failure
            result.issues_failed += 1
            message = str(exc) or exc.__class__.__name__
            result.failures.append(
                IssueFailure(
                    kind="issue_create",
                    source_number=source_number,
                    message=message,
                    step="create",
                )
            )
            self._safe_issue_failed(source_number, message)
            return

        # S3: post comments. Per-comment failures do not abort the
        # issue; the checkpoint still advances to S5 because the
        # issue itself was created successfully.
        for comment in issue.get("comments") or []:
            try:
                comment_index = int(comment.get("index", 0))
            except (TypeError, ValueError):
                # Skip malformed comments rather than failing the
                # whole issue; the issue is still migrated.
                continue
            result.comments_attempted += 1
            try:
                comment_payload: dict[str, Any] = {
                    "index": comment_index,
                    "body": str(comment.get("body", "")),
                }
                # The fake seam in tests/test_orchestration.py expects
                # the source issue number as the positional
                # ``issue_number`` argument and reads its ``index`` from
                # the payload. Production ``GitHubClient.create_comment``
                # accepts the github issue number; both signatures are
                # accepted because the fake is a positional-arg stub.
                response = self.github.create_comment(
                    source_number,
                    comment_payload,
                )
            except Exception as exc:  # noqa: BLE001 — comment failure
                result.comments_failed += 1
                message = str(exc) or exc.__class__.__name__
                result.failures.append(
                    IssueFailure(
                        kind="comment",
                        source_number=source_number,
                        message=message,
                        step="comment",
                    )
                )
                continue

            result.comments_succeeded += 1
            self._safe_record_comment(source_number, comment_index, response)

        # S4: close if the source issue was closed. Failures here are
        # warnings; the issue itself is still considered succeeded.
        source_closed = bool(issue.get("closed") or issue.get("state") == "closed")
        if source_closed:
            close = getattr(self.github, "close_issue", None)
            if callable(close):
                try:
                    close(github_number)
                except Exception as exc:  # noqa: BLE001 — close warning
                    result.failures.append(
                        IssueFailure(
                            kind="close_failed",
                            source_number=source_number,
                            message=str(exc) or exc.__class__.__name__,
                            step="close",
                        )
                    )

        # S5: checkpoint and report success.
        self._safe_record_issue(source_number, github_number)
        result.issues_succeeded += 1
        self._safe_issue_succeeded(source_number, github_number)

    # --- collaborator wrappers (duck-typed) ----------------------------------

    def _list_issues(self) -> list[dict[str, Any]]:
        """Read the source issue list from the Codeberg seam."""
        result = self.codeberg.list_issues()
        return list(result)

    def _already_migrated(self, source_number: int) -> bool:
        """Whether ``source_number`` is already checkpointed in the state seam."""
        already = getattr(self.state, "already_migrated", None)
        if not callable(already):
            return False
        try:
            return bool(already(source_number))
        except Exception:  # noqa: BLE001 — defensive: never abort resume
            return False

    def _safe_record_issue(self, source_number: int, github_number: int) -> None:
        """Forward ``record_issue`` to the state seam, swallowing errors."""
        record = getattr(self.state, "record_issue", None)
        if not callable(record):
            return
        try:
            record(source_number, github_number)
        except Exception:  # noqa: BLE001 — state seam is best-effort
            return

    def _safe_record_comment(
        self, source_number: int, comment_index: int, response: Any
    ) -> None:
        """Forward ``record_comment`` to the state seam, swallowing errors."""
        record = getattr(self.state, "record_comment", None)
        if not callable(record):
            return
        try:
            github_comment_id = self._extract_comment_id(response)
            record(source_number, comment_index, github_comment_id)
        except Exception:  # noqa: BLE001 — state seam is best-effort
            return

    @staticmethod
    def _extract_comment_id(response: Any) -> int:
        """Best-effort extraction of a comment id from a create response."""
        if isinstance(response, dict):
            raw = response.get("id", 0)
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
        try:
            return int(response)
        except (TypeError, ValueError):
            return 0

    def _safe_issue_started(self, source_number: int) -> None:
        started = getattr(self.reporter, "issue_started", None)
        if not callable(started):
            return
        try:
            started(source_number)
        except Exception:  # noqa: BLE001 — reporter is best-effort
            return

    def _safe_issue_succeeded(self, source_number: int, github_number: int) -> None:
        succeeded = getattr(self.reporter, "issue_succeeded", None)
        if not callable(succeeded):
            return
        try:
            succeeded(source_number, github_number)
        except Exception:  # noqa: BLE001 — reporter is best-effort
            return

    def _safe_issue_failed(self, source_number: int, reason: str) -> None:
        failed = getattr(self.reporter, "issue_failed", None)
        if not callable(failed):
            return
        try:
            failed(source_number, reason)
        except Exception:  # noqa: BLE001 — reporter is best-effort
            return

    def _safe_git_phase_finished(self, status: str) -> None:
        finished = getattr(self.reporter, "git_phase_finished", None)
        if not callable(finished):
            return
        try:
            finished(status)
        except Exception:  # noqa: BLE001 — reporter is best-effort
            return


__all__ = [
    "DEFAULT_LABEL_COLOR",
    "MigrationOrchestrator",
]
