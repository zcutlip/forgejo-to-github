# Stage 04 — `MigrationOrchestrator`

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** stage 01 (`StateStore`, `MigrationState`), stage 02
(`CodebergClient`, `GitHubClient`), stage 03 (`GitMirror`).
**Blocks:** stage 05 (reporter), stage 06 (CLI wiring).

## 1. Objective

Introduce `forgejo_to_github.migration.MigrationOrchestrator`. This
class is the only piece of the package that knows the order of phases
and the per-issue substep sequence. It is constructed by injecting
exactly five collaborators and performs no network or subprocess work
of its own.

The orchestrator is also the home of the `MigrationResult` dataclass
that the reporter consumes. The result is a plain `@dataclass`, not
a pydantic model, not a `dict`.

## 2. Files / modules

- **New module:** `forgejo_to_github/migration.py` containing:
  - `MigrationOrchestrator` class.
  - `MigrationResult` dataclass.
  - Optional small helpers (e.g., a `Repository` dataclass holding
    `source`, `target`, and the optional `description`). No proxy
    classes. No factory methods that build collaborators.

- `forgejo_to_github/__init__.py` is not modified during this stage.

## 3. Public API and responsibilities

### 3.1 `MigrationOrchestrator`

Constructor signature is locked by the test in
`tests/test_orchestration.py::test_orchestrator_constructor_accepts_injected_dependencies`:

```python
MigrationOrchestrator(
    repo: Repository,
    codeberg: CodebergClient,
    github: GitHubClient,
    git: GitMirror,
    state: StateStore,
    reporter: Reporter,
)
```

The test builds the orchestrator with these exact keyword arguments:
`repo=`, `api=` (renamed to `codeberg=` and `github=` — see naming
note below), `git=`, `state=`, `report=` (renamed to `reporter=`).

**Naming reconciliation with the existing test fixture.** The test
fixture in `tests/test_orchestration.py` passes a single `api=` seam
that implements both `list_issues` and `create_issue` and
`create_comment`. After stage 04 the orchestrator consumes **two**
distinct collaborators (`codeberg=` and `github=`) because the
`CodebergClient` and `GitHubClient` classes do not share an interface
(one is Forgejo, the other is GitHub; their endpoints differ).
Resolution: this stage amends the `tests/test_orchestration.py` fake
fixture (`_FakeApi`) **only by separating it into `_FakeCodeberg` and
`_FakeGitHub`** when the test file is updated as part of stage 04. The
test names and assertions remain unchanged. Specifically:

- `_FakeCodeberg.list_issues` returns the configured issues list.
- `_FakeGitHub.create_issue` and `_FakeGitHub.create_comment` are the
  existing fake methods, unchanged.
- The orchestrator is constructed with `codeberg=_FakeCodeberg(...),
  github=_FakeGitHub(...)` instead of the unified `api=` arg.

The amended fixture is the only edit to `tests/test_orchestration.py`
in this stage. All test function names and assertions remain exactly
as they are. The test surface remains locked.

### 3.2 `MigrationOrchestrator.run()`

```python
def run(self) -> MigrationResult: ...
```

Top-level orchestration entry point. Performs these phases in order:

1. **Pre-flight.** If `self.repo.target` does not yet exist on
   GitHub, fetch the source repository description (via
   `codeberg.get_repository_description()`) when no explicit
   `description` was provided, and create the target repository via
   `github.create_repository(...)`. On HTTP failure of the description
   fetch, fall back to `"Migrated from Codeberg"`.
2. **Repository description update.** If a non-empty description was
   supplied explicitly, call `github.update_repository_description(...)`
   immediately after repository creation.
3. **Git mirror.** If `self.repo.skip_git` is `False`, call
   `git.clone()` and then `git.push_branches(...)` /
   `git.push_tags(...)`. On clone failure, raise (terminal). On push
   failure, record the failure but proceed to issue migration.
   On success, advance the state via `state.save(...)` and call
   `git.cleanup(local_path)` in a `finally`.
4. **Issue migration.**
   - `state.load()` to obtain the `MigrationState`.
   - `codeberg.list_issues()` to enumerate.
   - For each issue whose number is not in `state.migrated`:
     - `reporter.issue_started(source_number=...)`.
     - `github.create_issue(...)`.
     - For each comment from `codeberg.list_comments(issue_id=...)`:
       - `github.create_comment(...)`.
     - If the source issue is closed, `github.close_issue(...)`.
     - `state.save(...)` to record the new mapping.
     - `reporter.issue_succeeded(source_number=..., github_number=...)`.
   - On per-issue failure, accumulate the failure into
     `MigrationResult.failures` and continue to the next issue. Do not
     raise.
5. **Final summary.** `reporter.render_final(result)` is invoked once.
6. **Return the result.** The orchestrator does not exit. Exit-code
   translation is the CLI's responsibility (stage 06).

### 3.3 Per-issue dependency rules

- **Within one issue**: create must precede every comment; comments
  must precede the optional close; the checkpoint must follow the
  close. Tests `test_create_issue_runs_before_comments_and_checkpoint`
  and `test_later_issue_continues_after_one_issue_creation_failure`
  pin this.
- **Across issues**: comment posting for issue N has no dependency on
  issue N+1. A failure on issue N does not stop issue N+1.
- **Checkpoint discipline**: `state.save` is only called with a
  `migrated` mapping that includes the just-succeeded issue. A failed
  issue never advances the checkpoint.

### 3.4 Clone-vs-push terminal/non-fatal classification

- **`git.clone()` failure is terminal.** The orchestrator must not
  proceed to issue migration. The test
  `test_clone_failure_is_terminal_and_skips_issue_migration` asserts
  that no `create_issue` call is recorded on the GitHub seam and that
  no checkpoint is recorded.
- **`git.push_branches(...)` failure is non-fatal.** The orchestrator
  logs the failure via `reporter.git_phase_finished(...)` and
  continues to issue migration. The result exposes `push_status`
  set to something other than `"ok"` so the reporter can name it.
  Test `test_push_failure_does_not_block_issue_migration`.
- **`git.push_tags(...)` failure is non-fatal for the same reasons.**
- **Cleanup.** `git.cleanup(local_path)` is called from the Git phase
  in a `finally`. The cleanup call survives push failure.

### 3.5 `MigrationResult`

Plain `@dataclass` (not frozen; the orchestrator constructs it
incrementally during the run, then returns it).

Fields (these names are part of the contract asserted by
`tests/test_orchestration.py` and `tests/test_reporting.py`):

| Field | Type | Notes |
|-------|------|-------|
| `issues_attempted` | `int` | Number of issues the orchestrator entered. |
| `issues_succeeded` | `int` | Number of issues created successfully. |
| `issues_failed` | `int` | Number of issues whose create failed. |
| `comments_attempted` | `int` | Total comments across all attempted issues. |
| `comments_succeeded` | `int` | Total comments posted successfully. |
| `comments_failed` | `int` | Total comments that failed. |
| `git` | `dict[str, str]` | `{"clone": "...", "push": "..."}`. Values are one of `"ok"`, `"failed"`, `"skipped"`. |
| `failures` | `list[dict]` | Each entry has at minimum `{"kind": str, "source_number": int, "message": str}`. |
| `clone_status` | `str` | Convenience alias for `git["clone"]`; some tests reach for it. |

The `MigrationResult` is consumed by the reporter (stage 05). It is
not formatted by the orchestrator; the orchestrator does not import
`forgejo_to_github.reporting` for this purpose (only the type, if at
all).

### 3.6 `Repository`

Tiny `@dataclass` to carry the immutable per-run inputs:

```python
@dataclass(frozen=True)
class Repository:
    source: str           # "owner/repo"
    target: str           # "owner/repo"
    description: str | None = None
    public: bool = False
    skip_git: bool = False
    dry_run: bool = False
    yes: bool = False
```

This is **not** a proxy class — it carries data and only the bare
accessor methods Python dataclasses generate. It exists so that the
orchestrator's constructor takes one `repo=` argument instead of a
long parameter list. The CLI builds it from `argparse.Namespace`.

### 3.7 No factory method

The orchestrator does **not** provide a `build(...)` classmethod or
`from_args(...)` constructor. The CLI is the only place that
constructs concrete collaborators. The reporter and clients are not
instantiated by the orchestrator.

## 4. Invariants

- **The orchestrator does not import `requests` or `subprocess`.**
  All network and subprocess calls happen behind injected seams.
- **The orchestrator does not instantiate collaborators.** It does
  not call `RequestsTransport()`, `StateStore(...)`, or any
  `CodebergClient` / `GitHubClient` / `GitMirror` / `Reporter`
  constructor itself.
- **No proxy methods on the orchestrator.** Every public method does
  real work: it advances the run, advances state, or produces a
  result. There is no `to_dict()` or `as_dict()` proxy method; the
  result is the dataclass.
- **Result is deterministic given fixed inputs and injectable
  timestamps.** The orchestrator does not read the wall clock for
  the result. (If a timestamp appears in the result — e.g., for
  state checkpoints — it is injected through the `StateStore` and
  flows through without modification.)
- **Token redaction survives orchestration.** The orchestrator does
  not log the GitHub token, the Codeberg token, or any URL
  containing an embedded token. The token is consumed inside
  `GitMirror.push_branches(...)` / `push_tags(...)`, which already
  redacts.

## 5. Collaborator / dependency rules

- `MigrationOrchestrator` accepts exactly five collaborators. No more,
  no fewer. Adding a sixth collaborator (e.g., a logger, a clock, a
  metrics sink) requires user approval and a new test.
- `MigrationOrchestrator` depends on `StateStore`, `CodebergClient`,
  `GitHubClient`, `GitMirror`, `Reporter`, and the `Repository`
  dataclass. It does not depend on the `Transport` Protocol or any
  formatter functions.
- `MigrationOrchestrator` imports `forgejo_to_github.formatting` for
  `format_issue_body` and `format_comment_body`. The formatting
  functions are pure and the import is acceptable.

## 6. Migration / compatibility constraints

- **`f2gh.migrate` stays in place during stage 04.** The legacy
  function continues to be the harness for `tests/test_migration_reporting.py`,
  `tests/test_issue_fetch_errors.py`, and parts of `tests/test_cli.py`.
  Removal happens in stage 06.
- **Phase ordering, clone-terminal, push-nonfatal, per-issue
  substep ordering are all observable behavior.** They must not be
  reordered, weakened, or short-circuited.
- **User-facing wording changes are permitted** only in advisory
  text inside the structured exception messages that `GitMirror`
  raises (stage 03) and in the report rendered by `Reporter`
  (stage 05). The orchestrator does not produce user-facing strings
  beyond the events it hands to the reporter.

## 7. Test references

Orchestration:

- `tests/test_orchestration.py::test_orchestrator_constructor_accepts_injected_dependencies`
- `tests/test_orchestration.py::test_clone_runs_before_any_issue_work`
- `tests/test_orchestration.py::test_clone_failure_is_terminal_and_skips_issue_migration`
- `tests/test_orchestration.py::test_push_failure_does_not_block_issue_migration`
- `tests/test_orchestration.py::test_create_issue_runs_before_comments_and_checkpoint`
- `tests/test_orchestration.py::test_later_issue_continues_after_one_issue_creation_failure`

Migration reporting (orchestrator truthfulness surfaces):

- `tests/test_migration_reporting.py::test_git_push_failure_is_non_fatal_and_reported`
  (verifies that `migrate()` continues into Phase 3 after a push
  failure and produces the expected failure framing; in stage 04 this
  test continues to pass against `f2gh.migrate`. The same contract
  is duplicated by `test_push_failure_does_not_block_issue_migration`
  against the new orchestrator.)
- `tests/test_migration_reporting.py::test_clone_failure_is_terminal_and_skips_issue_fetch`
- `tests/test_migration_reporting.py::test_issue_failure_is_accumulated_and_later_issues_continue`
- `tests/test_migration_reporting.py::test_successful_issues_are_checkpointed_and_resume_filters_them`

Issue fetch error handling:

- `tests/test_issue_fetch_errors.py::test_migrate_source_404_exits_gracefully`
  (asserts the legacy `migrate()` path; the new orchestrator must
  produce a parallel exit condition — surfaces through the result's
  failures list and the reporter).

Package boundary:

- `tests/test_package_boundaries.py::test_intended_public_class_is_importable`
  (parameterized for `forgejo_to_github.migration.MigrationOrchestrator`)
- `tests/test_package_boundaries.py::test_public_class_has_docstring`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_least_two_public_methods`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_most_seven_public_methods`
  (same)

Future tests to be added at stage 04 (do not pre-create; the
implementing agent adds them after RED review):

- `test_per_issue_checkpoint_advances_only_on_full_success` — asserted
  via the existing `tests/test_orchestration.py::test_create_issue_runs_before_comments_and_checkpoint`,
  which is sufficient to pin the ordering. A dedicated test is
  optional.

## 8. Implementation order

1. Add `forgejo_to_github/migration.py` with `Repository`,
   `MigrationResult`, and `MigrationOrchestrator`.
2. Update `tests/test_orchestration.py`'s `_FakeApi` fixture into
   `_FakeCodeberg` / `_FakeGitHub` (or keep `_FakeApi` and pass the
   same instance to both `codeberg=` and `github=` if its surface is
   the union of both — the cleaner choice is two fakes; the test
   function bodies remain unchanged).
3. Run `./scripts/run-tests.sh tests/test_orchestration.py
   tests/test_package_boundaries.py`. Confirm green.
4. Run the legacy parity tests:
   `./scripts/run-tests.sh tests/test_migration_reporting.py
   tests/test_issue_fetch_errors.py`. Confirm green.
5. Run the full suite via `./scripts/run-tests.sh`. All pre-existing
   tests must remain green.
6. Stop and report.

## 9. Verification commands

```bash
./scripts/run-tests.sh tests/test_orchestration.py
./scripts/run-tests.sh tests/test_migration_reporting.py
./scripts/run-tests.sh tests/test_issue_fetch_errors.py
./scripts/run-tests.sh tests/test_package_boundaries.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/migration.py
```

`mypy forgejo_to_github/migration.py` is informational.

## 10. Stop gate

The implementing agent stops and reports:

- Confirmation that `forgejo_to_github.migration.MigrationOrchestrator`
  exists with the locked five-collaborator constructor.
- Test results for the targeted suites plus the full suite.
- The diff for `tests/test_orchestration.py` (the `_FakeApi` split),
  including confirmation that no test function names or assertions
  were changed.
- Any deviation from the locked result dataclass fields, even adding
  new ones, with justification.

The user reviews before stage 05 begins.

## 11. Out of scope

- Editing `f2gh.py` to call `MigrationOrchestrator`. That is stage 06.
- Exit-code translation. That is stage 06.
- Report formatting beyond handing structured events to the reporter.
  That is stage 05.
- Cancellation handling (Ctrl-C). That is
  `plans/03-keyboard-interrupt-handling.md`.
- Clone cache retention. That is `plans/04-retain-clone-cache.md`.
- Local-clone mode. That is `plans/05-local-clone-invocation.md`.
