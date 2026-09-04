"""Domain types for ``forgejo_to_github``.

This module defines the value objects and result dataclasses that flow
between the orchestrator, the reporter, and the CLI. They contain data
only; no I/O is performed here.

Three public dataclasses are exported:

- :class:`Repository` — immutable per-run inputs (frozen).
- :class:`IssueFailure` — structured record of one failure.
- :class:`MigrationResult` — aggregated orchestrator output.
- :class:`DryRunDiscovery` — dry-run discovery facts (frozen).

These types are part of the locked public contract asserted by
``tests/test_orchestration.py`` and ``tests/test_reporting.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Repository:
    """Immutable per-run migration inputs.

    Constructed by the CLI from ``argparse.Namespace`` and passed by
    value to the orchestrator. Frozen so it cannot be mutated mid-run.

    Attributes:
        source: Codeberg/Forgejo repository as ``"owner/repo"``.
        target: GitHub repository as ``"owner/repo"``.
        description: Optional explicit description override.
        public: When ``True``, create the target repository as public.
        skip_git: When ``True``, skip the Git mirror phase entirely.
        dry_run: When ``True``, the run is read-only, not offline: only
            GET discovery requests are issued — no mutating HTTP, no
            git subprocess, and no checkpoint writes.
        yes: When ``True``, skip any interactive confirmation prompts.
    """

    source: str
    target: str
    description: str | None = None
    public: bool = False
    skip_git: bool = False
    dry_run: bool = False
    yes: bool = False


@dataclass
class IssueFailure:
    """Structured record of a single failure observed during migration.

    Used by the orchestrator to populate ``MigrationResult.failures``
    and by the reporter to name each failure in the final summary.

    Attributes:
        kind: Coarse-grained failure category (for example
            ``"issue_create"``, ``"comment"``, ``"close_failed"``).
        source_number: The Codeberg/Forgejo issue number affected.
        message: Human-readable, redaction-safe description. The
            orchestrator never embeds credentials here.
        step: Fine-grained substep (for example ``"create"``,
            ``"comment"``, ``"close"``, ``"label"``).
    """

    kind: str
    source_number: int
    message: str
    step: str


@dataclass(frozen=True)
class DryRunDiscovery:
    """Discovery facts gathered by the dry-run read-only short-circuit.

    Populated only by the dry-run discovery phase and attached to
    :attr:`MigrationResult.discovery`; it is ``None`` on normal runs.
    Frozen so the discovery facts cannot be mutated once produced.

    Attributes:
        target: GitHub repository as ``"owner/repo"`` as supplied by
            the CLI via :class:`Repository.target`.
        repo_exists: ``True`` when the read-only ``GET`` against the
            target repository found it; ``False`` when not found. No
            repository is created.
        comments_discovered: Total comments across the discovered
            source issues, read-only. No comment is posted.
        state_path: The state file path held by the ``StateStore``.
            The store is used read-only: state is loaded, never
            written.
        state_migrated: Number of issues checkpointed in the loaded
            state's ``migrated`` mapping.
    """

    target: str
    repo_exists: bool
    comments_discovered: int
    state_path: str
    state_migrated: int


@dataclass
class MigrationResult:
    """Aggregated output of one orchestrator run.

    Constructed incrementally by the orchestrator and returned from
    :meth:`MigrationOrchestrator.run`. The reporter consumes this
    object to render the final summary; the CLI translates it into a
    process exit code.

    The ``git`` mapping carries the per-phase status (``"ok"``,
    ``"failed"``, or ``"skipped"``) and ``clone_status`` /
    ``push_status`` are convenience aliases that mirror its entries.

    ``issues_discovered`` is recorded only by the dry-run read-only
    discovery phase: it carries the number of source issues found by
    the read-only listing while ``issues_attempted`` stays ``0``
    (discovery is not an attempt). It remains ``0`` on normal runs.

    ``discovery`` carries the dry-run discovery facts (see
    :class:`DryRunDiscovery`); it is populated only on a dry run and
    stays ``None`` on normal runs.
    """

    issues_attempted: int = 0
    issues_succeeded: int = 0
    issues_failed: int = 0
    issues_discovered: int = 0
    comments_attempted: int = 0
    comments_succeeded: int = 0
    comments_failed: int = 0
    git: dict[str, str] = field(
        default_factory=lambda: {"clone": "skipped", "push": "skipped"}
    )
    failures: list[IssueFailure] = field(default_factory=list)
    clone_status: str = "skipped"
    push_status: str = "skipped"
    dry_run: bool = False
    discovery: DryRunDiscovery | None = None
