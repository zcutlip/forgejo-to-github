# Package refactor and test foundation

**GitHub issue:** [#3](https://github.com/zcutlip/forgejo-to-github/issues/3)

## Purpose

Refactor the monolithic `f2gh.py` script into a small, multi-file Python package
with clear object-oriented boundaries and a proper automated test suite. The
refactor should preserve the existing CLI behavior and migration semantics while
making failure handling, state transitions, and future features easier to reason
about and test.

This plan follows `plans/01-clone-failure-followup.md`. Its core clone-failure
fix is already complete; the remaining verification work from plan 01 should be
captured by the tests added here.

## Goals

- Preserve the `f2gh` console entry point and existing command-line behavior
  unless an intentional change is documented.
- Establish focused module boundaries instead of growing `f2gh.py` further.
- Apply sound object-oriented design without creating a god object or needless
  abstraction. Classes should have one clear responsibility and depend on
  interfaces/protocols where that improves testability.
- Make API, Git, state, orchestration, and reporting behavior independently
  testable.
- Keep external dependencies minimal and target Python 3.10+.
- Keep state human-readable and atomically written with `os.replace`.

## Proposed boundaries

The exact names can be adjusted during implementation, but the responsibilities
should remain distinct:

- **CLI** — argument parsing, command entry point, and exit-code translation.
- **Configuration/domain models** — validated source/target repositories,
  migration options, migration results, and structured failures.
- **Codeberg client** — repository metadata, issues, and comments.
- **GitHub client** — repository creation and issue/comment/label operations.
- **Git mirror service** — clone, branch/tag pushes, cleanup, redaction, and
  Git-specific failure classification.
- **State store** — loading, validating, checkpointing, and atomically saving
  `state.json`.
- **Migration orchestrator** — phase ordering, per-issue dependency handling,
  resumption, and aggregation of results.
- **Reporter** — human-readable progress, final status, actionable advice, and
  consistent success/incomplete/failure outcomes.

Use dependency injection for network clients, Git command execution, state
storage, and output where practical. Tests must be able to run without network
access or real credentials.

## Work sequence

1. **Characterize current behavior.** Add tests around state loading/saving,
   atomic replacement, CLI options, formatting, repo-description fallback,
   issue resumption, and final status reporting.
2. **Add failure-path regression tests.** Cover clone failure, branch push
   failure, tag push failure, workflow-scope advice, token redaction, Git
   continuation to issue migration, per-issue error accumulation, and truthful
   final reporting.
3. **Extract pure/domain code first.** Move formatting, validation, error/result
   models, and state persistence without changing behavior.
4. **Extract API clients and Git service.** Keep transport and subprocess details
   behind narrow interfaces that can be replaced by test doubles.
5. **Extract orchestration and reporting.** Make phase dependencies explicit:
   repository setup is required before migration, Git is important but does not
   block independent issue migration, and issue substeps remain dependent within
   one issue.
6. **Preserve the entry point.** Keep `f2gh` and the documented invocation
   working through the new package, with `f2gh.py` retained only as a deliberate
   compatibility shim if needed.
7. **Run the full verification suite.** Use mocked API/subprocess interactions;
   do not contact Codeberg or GitHub during tests. Run `pytest`, `ruff check .`,
   and `mypy` for the resulting package.

## Design constraints

- Do not change migration semantics merely to make extraction easier.
- Do not add a class whose only purpose is to wrap one unrelated function.
- Keep error types structured until the presentation boundary; do not use
  formatted strings as control flow.
- Preserve token redaction at every subprocess/error boundary.
- Preserve per-issue checkpoints and deterministic resume behavior.
- Keep the current `--skip-git`, `--dry-run`, and explicit source/target options
  compatible while later plans may add local-checkout behavior.
- Do not touch pre-commit configuration unless explicitly requested.

## Completion criteria

- The package has clear ownership of CLI, API, Git, state, orchestration, and
  reporting responsibilities.
- The `f2gh` entry point works without behavior regressions.
- Tests cover both successful and partial-failure paths, including clone/push
  errors and stateful resume.
- No test requires live credentials or network access.
- `pytest`, `ruff check .`, and `mypy` pass.
- Plans 03–05 can build on the new boundaries without returning logic to a
  monolithic script.

## Dependencies

- Follows `plans/01-clone-failure-followup.md`.
- Must precede keyboard-interrupt handling, clone retention, and local-checkout
  optimization because those features cross orchestration, Git, state, cleanup,
  and reporting boundaries.

## References

- GitHub issue #1: https://github.com/zcutlip/forgejo-to-github/issues/1
- `plans/01-clone-failure-followup.md`
