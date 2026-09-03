# Stage 05 — Reporter

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** stage 04 (`MigrationResult`, `IssueFailure`).
**Blocks:** stage 06 (CLI wiring).

## 1. Objective

Replace the inline reporting code at the bottom of `f2gh.migrate`
(the `print` block that writes the migration summary and the
`Resume:` lines) with a `Reporter` class. The reporter consumes the
`MigrationResult` produced by `MigrationOrchestrator` and writes a
truthful, concise final summary. It also handles progress events
during the run.

The reporter's responsibilities are:

- Accept two injected output `Sink` Protocols (one for normal output,
  one for error output) so tests can capture output without touching
  `sys.stdout` or `sys.stderr`.
- Default the normal-output sink to a thin wrapper around
  `sys.stdout` and the error-output sink to a thin wrapper around
  `sys.stderr`.
- Render progress events handed to it by the orchestrator. The
  reporter decides which sink to use per event.
- Render a final summary that:
  - Reports the issue and comment counts truthfully.
  - Names every failure exactly once.
  - Distinguishes complete success from partial failure from terminal
    failure from dry-run.
  - Surfaces a Git push failure concisely, **without** replaying the
    full multi-line advisory block.
  - Surfaces a Git clone failure by name.
  - Never claims "All migrated" when a failure is present.
- Expose an `exit_outcome(result: MigrationResult) -> int` method that
  returns the CLI exit code for the run (0, incomplete, failure). The
  CLI translates this into `sys.exit`.

## 2. Files / modules

- **New module:** `forgejo_to_github/reporting.py` containing:
  - `Sink` Protocol.
  - `StdoutSink` and `StderrSink` default implementations.
  - `Reporter` class.
  - Module-private constants for exit codes (named, not magic numbers).

- `forgejo_to_github/__init__.py` is not modified during this stage.

## 3. Public API and responsibilities

### 3.1 `Sink` Protocol

```python
class Sink(Protocol):
    def write(self, line: str) -> None: ...
```

A sink is anything with a `write(line: str) -> None` method. The test
fixture in `tests/test_reporting.py` is a `_Sink` class with that exact
signature plus a `text()` accessor.

The default normal sink writes each line followed by a single newline
to `sys.stdout`. The default error sink writes each line followed by a
single newline to `sys.stderr`. Internally:

```python
class StdoutSink:
    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def write(self, line: str) -> None:
        self._stream.write(line + "\n")


class StderrSink:
    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stderr

    def write(self, line: str) -> None:
        self._stream.write(line + "\n")
```

`Reporter` accepts `output: Sink | None = None` and
`error_output: Sink | None = None` arguments. When `None`, the
constructor instantiates `StdoutSink()` / `StderrSink()` respectively.

### 3.2 `Reporter`

Constructor:

```python
Reporter(output: Sink | None = None, error_output: Sink | None = None)
```

Methods:

| Method | Sink | Purpose |
|--------|------|---------|
| `run_started(total: int)` | `output` | Emit a "Starting migration of N issues" line. |
| `issue_started(source_number: int, total: int)` | `output` | Emit a "Migrating Issue #N" line. The `total` argument is the number of issues to migrate in this run, used for the `N/M` progress format. |
| `issue_succeeded(source_number: int, github_number: int)` | `output` | Emit a "Created issue #M on GitHub" line. |
| `issue_failed(source_number: int, kind: str, message: str)` | `error_output` | Emit a "FAILED [kind] CB #N: message" line. |
| `git_phase_finished(status: str)` | `output` or `error_output` based on status | Emit a one-line summary of the Git phase. `"failed"` routes to `error_output`; `"ok"` and `"skipped"` route to `output`. |
| `render_final(result: MigrationResult)` | both sinks, mixed based on success/failure | Emit the final summary. Idempotent in that calling it twice yields two full summaries (the CLI calls it exactly once). The summary header and counters go to `output`; failure listings and advisory-named lines go to `error_output`. |
| `exit_outcome(result: MigrationResult) -> int` | n/a | Return 0 on complete success, the documented "incomplete" code on partial failure, the documented "failure" code on terminal failure, and 0 on dry-run regardless of underlying state. The CLI maps this to `sys.exit`. |

The `issue_failed` method receives a structured `kind` (e.g.,
`"issue_create"`, `"comment"`, `"close_failed"`) and a `message`.
The reporter formats them into the failure line. The reporter
**does not** receive the title (titles are not preserved in the
failure path; the legacy code logged them but they were inconsistently
populated and this is a deliberate cleanup).

### 3.3 Exit-code constants

Module-level named constants (not magic numbers):

```python
EXIT_SUCCESS: int = 0
EXIT_INCOMPLETE: int = 1
EXIT_FAILURE: int = 2
```

The exact values are chosen for the spec. The legacy code's
`SystemExit(_ExitMessage(msg))` produces exit code 1 for any
exception path. The new exit codes distinguish:

- `EXIT_SUCCESS` (0) — all issues migrated (or, in dry-run, a clean
  dry-run).
- `EXIT_INCOMPLETE` (1) — some issues migrated, some failed; the
  migration is partial.
- `EXIT_FAILURE` (2) — terminal failure before any issues were
  attempted (e.g., clone failure, source 404).

A dry-run that fails input validation in the CLI still exits with
the validation code (2) before reaching the orchestrator.

### 3.4 Truthfulness rules

The final summary must obey:

1. When `result.issues_failed == 0` and `result.git["clone"] == "ok"`
   and `result.git["push"] in ("ok", "skipped")` and there are no
   entries in `result.failures`, the summary contains the substring
   `"migrated"` and either `"all"` or `"complete"` (per
   `test_complete_result_reports_complete_migration`).
2. When any failure is present, the summary must NOT contain the
   substring `"all migrated"` (per
   `test_result_with_failure_does_not_claim_all_migrated`).
3. The failed count (`issues_failed + comments_failed`) must be
   surfaced, and the number `N` (when there are `N` failures in
   `result.failures`) must appear at least once in the summary (per
   `test_report_names_every_failure_exactly_once`).
4. When `result.git["push"] == "failed"`, the summary must include a
   concise status line and must not echo the multi-line advisory
   block. Specifically, the substrings `"Possible causes:"`,
   `"Remediation:"`, `"git pull --rebase"`, and `"--force-with-lease"`
   must not appear in the summary (per
   `test_git_push_failure_summary_does_not_replay_multiline_advisory`).
5. When `result.git["clone"] == "failed"`, the summary must include
   `"clone"` and `"fail"` substrings (per
   `test_clone_failure_summary_marks_clone_status_distinctly`).
6. When `result.dry_run is True`, the summary is the approved
   informative dry-run preview. It is rendered from
   `result.discovery` (the `DryRunDiscovery` value from
   stage 04 §3.7.1) and `result.issues_discovered`, and consists of
   these lines:

   ```
   Dry-run complete — no changes were made.
   Target repo: owner/target
   Repo: would be created
   Issues: would process N issues
   Comments: would post M
   Git: clone skipped, push skipped (dry-run)
   State: path (K checkpointed)
   ```

   The `Repo:` line reads `would be created` when
   `discovery.repo_exists` is `False` and `existing` when
   it is `True`. `N` is `result.issues_discovered`; `M` is
   `discovery.comments_discovered`; the `State:` path and `K`
   are `discovery.state_path` and
   `discovery.state_migrated`. The preview does not claim
   any issue was migrated and does not enumerate failures (per
   `test_dry_run_summary_does_not_claim_migrated`, to be added in
   stage 06), and it is written to the normal-output
   sink (a dry run produces no failures). The discovered issue count
   is reported via the `"would process N issues"` wording driven by
   `result.issues_discovered` (per
   `tests/test_orchestration.py::test_dry_run_reports_discovered_issue_count`).
   The template must not consume `issues_attempted` for this: on a
   dry run that counter is always `0`, and discovery is reported
   from `issues_discovered` instead.
7. The final summary is written to `output` on success and to
   `error_output` on any failure (per the new
   `test_reporter_writes_failure_summary_to_error_sink` to be added
   in stage 05).

### 3.5 Conciseness for push failures

The summary surfaces the push failure as one line naming the status.
The full advisory block is reachable from the orchestrator's events
(`reporter.git_phase_finished(status="failed")` writes a one-line
status; the advisory is part of the `GitPushError`'s `str()` and is
not replayed into the final summary by the reporter). The exact
wording of the push failure line is not locked; the constraint is the
absence of advisory substrings.

### 3.6 Token redaction

The reporter never includes the GitHub or Codeberg token in its
output. The orchestrator's contract already forbids passing tokens
into the reporter, and the reporter's templating must not import
or re-emit the auth URL. This invariant is implicit in the existing
test suite and is restated here for the spec.

## 4. Invariants

- **No proxy methods.** The reporter exposes the methods listed
  above and nothing else. There is no `to_dict()`, no `as_yaml()`,
  no `format_for_log()` proxy.
- **No pure-formatting class.** The reporter does not own formatting
  helpers that could live as functions. Formatting (Markdown body
  building, attribution block) stays in
  `forgejo_to_github.formatting` (already in place from before this
  plan).
- **No I/O at module import.** The default `StdoutSink` and
  `StderrSink` are constructed inside `Reporter.__init__` only when
  no sink is supplied.
- **Sink injection is mandatory for tests.** The reporter does not
  read or write `sys.stdout` or `sys.stderr` itself; the sinks
  encapsulate the destinations. Test fixtures substitute recording
  sinks.
- **No "no I/O at module import" over-reach.** The reporter may
  import `sys` for the default sinks; the rule is that no I/O
  happens at import time, not that the module is forbidden from
  importing I/O-related modules.

## 5. Collaborator / dependency rules

- `Reporter` accepts two collaborators: the two `Sink` instances. No
  state, no clients, no Git mirror.
- `Reporter` depends on `MigrationResult` (stage 04) for its input
  type. It does not depend on `StateStore`, `CodebergClient`,
  `GitHubClient`, or `GitMirror`.
- `Reporter` does not import `requests`, `subprocess`, `argparse`, or
  any I/O module at module scope other than `sys` (used by the
  default sinks). The default sinks are constructed only when
  explicitly requested.

## 6. Migration / compatibility constraints

- **The legacy `migrate()` final-report block stays in place during
  stage 05.** It is removed in stage 06 when the CLI is rewired to
  call `Reporter.render_final(result)` directly.
- **User-facing wording changes** in the final summary text are
  permitted provided the truthfulness rules above and the existing
  test contracts are preserved. The implementing agent must stop and
  surface any wording change that would alter an existing test
  assertion's substring check.

## 7. Test references

- `tests/test_reporting.py::test_reporter_constructor_accepts_injected_output_sink`
- `tests/test_reporting.py::test_complete_result_reports_complete_migration`
- `tests/test_reporting.py::test_result_with_failure_does_not_claim_all_migrated`
- `tests/test_reporting.py::test_report_names_every_failure_exactly_once`
- `tests/test_reporting.py::test_git_push_failure_summary_does_not_replay_multiline_advisory`
- `tests/test_reporting.py::test_clone_failure_summary_marks_clone_status_distinctly`

Added in stage 05:

- `tests/test_reporting.py::test_reporter_writes_failure_summary_to_error_sink` —
  asserts that when `result.failures` is non-empty, the final summary
  is written to the `error_output` sink and not to the `output`
  sink.

Added in stage 06:

- `tests/test_reporting.py::test_dry_run_summary_does_not_claim_migrated` —
  asserts the dry-run summary is the approved preview (stage 05
  §3.4 rule 6) rendered from `result.discovery` and does not
  contain "migrated" or "complete" as success claims. The preview
  renders the discovered count from `result.issues_discovered` using
  the "would process N issues" wording; `issues_attempted` is always
  `0` on a dry run and is never rendered as the dry-run count.

Package boundary:

- `tests/test_package_boundaries.py::test_intended_public_class_is_importable`
  (parameterized for `forgejo_to_github.reporting.Reporter`)
- `tests/test_package_boundaries.py::test_public_class_has_docstring`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_least_two_public_methods`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_most_seven_public_methods`
  (same)

Legacy parity (must remain green throughout this stage):

- `tests/test_migration_reporting.py::test_issue_failure_is_accumulated_and_later_issues_continue`
  (asserts the existing legacy framing; the new reporter's wording
  must remain compatible with the substring assertions).
- `tests/test_migration_reporting.py::test_git_push_failure_is_non_fatal_and_reported`
  (asserts `"Git: FAILED"` and absence of `"All issues migrated."`).

## 8. Implementation order

1. Add `forgejo_to_github/reporting.py` with `Sink`, `StdoutSink`,
   `StderrSink`, `Reporter`, and exit-code constants.
2. Add the new test
   `tests/test_reporting.py::test_reporter_writes_failure_summary_to_error_sink`
   as a RED test that fails meaningfully before implementation. The
   agent must stop and surface the RED state per
   `test-framework-spec.md` §15.
3. Run `./scripts/run-tests.sh tests/test_reporting.py
   tests/test_package_boundaries.py`. Confirm green.
4. Run the legacy parity tests:
   `./scripts/run-tests.sh tests/test_migration_reporting.py`.
   Confirm green.
5. Run the full suite via `./scripts/run-tests.sh`. All pre-existing
   tests must remain green.
6. Stop and report.

## 9. Verification commands

```bash
./scripts/run-tests.sh tests/test_reporting.py
./scripts/run-tests.sh tests/test_migration_reporting.py
./scripts/run-tests.sh tests/test_package_boundaries.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/reporting.py
```

`mypy forgejo_to_github/reporting.py` is informational.

## 10. Stop gate

The implementing agent stops and reports:

- Confirmation that `forgejo_to_github.reporting.Reporter` exists with
  the locked public surface.
- Test results for the targeted suites plus the full suite.
- The exact wording of the final summary for the five cases
  (complete success, issue failure, git push failure, git clone
  failure, dry-run), so the user can review and approve any wording
  change before stage 06 locks it in.
- Confirmation that no `f2gh.py` symbols were modified in this stage.

The user reviews before stage 06 begins.

## 11. Out of scope

- Editing `f2gh.py` to call `Reporter`. That is stage 06.
- Exit-code translation in the CLI. That is stage 06.
- Color output, terminal-width wrapping, ANSI handling. Not in this
  plan.
- Internationalization. Not in this plan.
- A `--quiet` flag. Not in this plan.
