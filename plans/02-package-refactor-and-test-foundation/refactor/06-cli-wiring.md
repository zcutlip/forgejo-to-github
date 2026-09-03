# Stage 06 — CLI wiring

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** stages 01–05 (every other package module).
**Blocks:** stage 07 (completion).

## 1. Objective

Rewire `f2gh.py` so it:

1. Continues to be the documented console-script entry point declared
   in `pyproject.toml` (`[project.scripts] f2gh = "f2gh:main"`).
2. Continues to expose `parse_args()` and `main()` callable by name,
   with the same external surface as before.
3. Constructs the five concrete collaborators (`CodebergClient`,
   `GitHubClient`, `GitMirror`, `StateStore`, `Reporter`) inside
   `main()`.
4. Constructs a `Repository` dataclass from the parsed `argparse.Namespace`.
5. Constructs a `MigrationOrchestrator` from the five collaborators
   and the `Repository`.
6. Calls `orchestrator.run()`, then `reporter.render_final(result)`,
   then `sys.exit(reporter.exit_outcome(result))`.
7. Removes the legacy module-level `migrate()`, `mirror_git_repo()`,
   `cb_headers()`, `gh_headers()`, `gh_request()`,
   `fetch_codeberg_description()`, `create_github_repo()`,
   `fetch_all_codeberg_issues()`, `fetch_codeberg_comments()`,
   `create_github_issue()`, `create_github_comment()`,
   `close_github_issue()`, `check_target_repo()`, `load_state()`,
   `save_state()`, `get_github_token()`, `codeberg_token()`, and the
   `_ExitMessage` helper — they are replaced by the package modules.
8. Preserves every CLI flag the existing CLI tests assert on.

The CLI is the **only** place that constructs concrete collaborators.
The orchestrator does not build them. Tests of the orchestrator
(stage 04) inject fakes; this stage is what wires the production
instances together.

## 2. Files / modules

- **`f2gh.py`** is modified in this stage. The set of modifications is
  bounded:
  - `parse_args()` stays (with the same argparse definition; the help
    text and flag set are unchanged).
  - `main()` is rewritten to build the collaborators and call the
    orchestrator.
  - The legacy `migrate()` and its helpers are removed.
  - `f2gh.py` continues to be importable as a top-level module so the
    `f2gh` console-script entry point and the `tests/test_cli.py`
    `import f2gh` work.
  - `f2gh.py` does **not** import `requests` at module scope. It
    imports `argparse`, `os`, `subprocess`, `sys`, `pathlib`, and the
    new package modules.

- **No other files modified** in this stage, except for the test
  rewrites described in §6.

- `pyproject.toml` is unchanged: `f2gh` is still the entry point;
  `pythonpath = ["."]` still lets `import f2gh` resolve.

## 3. Public API and responsibilities

### 3.1 `f2gh.parse_args() -> argparse.Namespace`

Unchanged signature and behavior. Same flags, same defaults, same
help text, same exit codes (argparse's standard 2 for argument
errors, 0 for `--help`).

The tests in `tests/test_cli.py` and `tests/test_characterization.py`
exercise this function and remain green.

### 3.2 `f2gh.main() -> None`

Rewritten. The function:

1. Calls `parse_args()`.
2. Resolves the state file path: `Path("state.json")` in the current
   working directory. (`--state-file` is **not** introduced in this
   plan; see `00-index.md` "State file path" decision.)
3. Reads the Codeberg token from `os.environ["CODEBERG_TOKEN"]`. If
   missing, raise `SystemExit("CODEBERG_TOKEN not set.")` — preserving
   the legacy behavior the existing `codeberg_token()` helper
   enforces.
4. Reads the GitHub token: prefer `os.environ["GITHUB_TOKEN"]`;
   fall back to `subprocess.run(["gh", "auth", "token"], ...)`; raise
   `SystemExit("GITHUB_TOKEN not set and 'gh auth token' failed.")`
   on failure — preserving the legacy behavior of
   `get_github_token()`.
5. Splits `args.source` and `args.target` on the first `/`. If either
   split yields fewer than two parts, raise
   `SystemExit("invalid source/target: ...")` with the same exit code
   as argparse argument errors (2). This matches the contract
   asserted by `test-framework-spec.md` §7.1
   (`source/target accept owner/repo form; bare names raise a structured
   parse error`).
6. Constructs:
   - `codeberg_transport = RequestsTransport()` (production default).
   - `github_transport = RequestsTransport()`.
   - `codeberg = CodebergClient(base_url="https://codeberg.org",
     owner=source_owner, repo=source_repo, token=codeberg_token,
     transport=codeberg_transport)`.
   - `github = GitHubClient(base_url="https://api.github.com",
     owner=target_owner, repo=target_repo, token=github_token,
     transport=github_transport)`.
   - `git = GitMirror(source_url=..., target_url=..., github_token=github_token)`.
   - `state = StateStore(state_path, source, target)`.
   - `reporter = Reporter()` (default `StdoutSink`/`StderrSink`).
7. Constructs `repo = Repository(source=..., target=..., description=...,
   public=args.public, skip_git=args.skip_git, dry_run=args.dry_run,
   yes=args.yes)`.
8. Constructs
   `orchestrator = MigrationOrchestrator(repo=repo, codeberg=codeberg,
   github=github, git=git, state=state, reporter=reporter)`.
9. Calls `result = orchestrator.run()`.
10. Calls `reporter.render_final(result)`.
11. Calls `sys.exit(reporter.exit_outcome(result))`.

The orchestrator owns the rest of the run; `main()` does not
implement any phase logic itself.

### 3.3 Dry-run override at the CLI

The CLI does **not** bypass orchestrator discovery on dry-run. It
constructs the orchestrator and calls `run()` exactly as in a normal
run; the orchestrator's dry-run short-circuit performs the read-only
discovery (read-only `GET` requests only; no mutating request, no git
subprocess, no state write) and records the found issue count in
`MigrationResult.issues_discovered` while keeping
`issues_attempted == 0`. The CLI's dry-run behavior is to:

1. Validate `args` (already done by `parse_args`).
2. Resolve the state path and read the Codeberg/GitHub tokens.
3. Construct a `Repository` with `dry_run=True`.
4. Construct the orchestrator and call `run()`.
5. Call `reporter.render_final(result)`.
6. Call `sys.exit(reporter.exit_outcome(result))` (which returns
   `EXIT_SUCCESS` for dry-run).

The CLI itself issues no HTTP request and invokes no git subprocess
during a dry-run; the read-only discovery is performed by the
orchestrator through the injected clients. The token reads remain
because they are local environment reads.

### 3.4 `f2gh._build_orchestrator(args: argparse.Namespace) -> MigrationOrchestrator`

A new private helper inside `f2gh.py`. It performs steps 3–8 of
`main()`. This isolates the wiring logic so it can be tested
directly. Its name is `_build_orchestrator` and it is the only
non-public symbol left in `f2gh.py` after this stage.

### 3.5 Compatibility preservation

The legacy public surface that tests pin:

| Legacy symbol | Replacement |
|---------------|-------------|
| `f2gh.parse_args` | kept; same signature |
| `f2gh.main` | kept; same name and zero-arg signature; rewired body |
| `f2gh.migrate(...)` | **removed**; tests in `tests/test_migration_reporting.py`, `tests/test_issue_fetch_errors.py` are rewritten in this stage to target the new orchestrator. See §6. |
| `f2gh.STATE_FILE` | **removed**; `tests/test_state.py::test_save_and_load_state_round_trip` is rewritten to use `StateStore(path, source, target)` directly. |
| `f2gh.load_state` / `f2gh.save_state` | **removed**; the legacy `tests/test_characterization.py` load/save tests are deleted because the `StateStore` tests in `tests/test_state_store.py` already cover the contract. See §6. |
| `f2gh.mirror_git_repo` | **removed**; the tests in `tests/test_git_errors.py` that exercise `f2gh.mirror_git_repo` via the CLI harness are rewritten to drive `GitMirror` directly with fake command runners. See §6. |
| `f2gh._ExitMessage` | **removed**; the `SystemExit(_ExitMessage(msg))` wrapper is replaced by `sys.exit(reporter.exit_outcome(result))`. |
| `f2gh._build_orchestrator` | **new private helper**; not part of any test surface. |

The legacy `f2gh` top-level module remains importable so the
`f2gh = "f2gh:main"` entry point resolves.

## 4. Invariants

- **`f2gh.py` imports no `requests` at module scope.** The default
  `RequestsTransport` adapter is the only place that touches the
  `requests` library, and it does so lazily inside its methods.
- **`f2gh.py` imports `subprocess` at module scope only for the
  `gh auth token` call.** The `subprocess.run` call to
  `["gh", "auth", "token"]` happens inside `main()` / `_build_orchestrator`,
  not at module import. The `import subprocess` line is allowed at
  the top of `f2gh.py` because no subprocess is spawned at import
  time; the run call is deferred to runtime.
- **The orchestrator is constructed exactly once per `main()`
  invocation.** There is no module-level orchestrator instance.
- **No collaborator leaks across invocations.** Each `main()` call
  builds fresh collaborators. State persists only in `state.json`
  on disk.
- **The CLI exit code is determined by `Reporter.exit_outcome`.**
  No `SystemExit(_ExitMessage(...))` wrapper remains. No hard-coded
  exit numbers in `main()`.

## 5. Collaborator / dependency rules

- `main()` is the only function that knows how to wire the
  collaborators together. Tests of the orchestrator (stage 04) do
  not call `main()`.
- `f2gh.py` may import from any module in `forgejo_to_github.*`. It
  may not import any submodule of the package back into itself
  (no circular reference).
- `f2gh.parse_args` may not import any package module. It is pure
  argparse logic.

## 6. Test rewrite and deletion protocol

The test framework spec (`test-framework-spec.md` §16) treats the
test suite as a locked contract. The refactor necessarily requires
amending or deleting some tests that exercise legacy `f2gh.py`
symbols that stage 06 removes. This section defines the formal
protocol that the implementing agent follows.

### 6.1 Approval gate

Before any test in this section is amended, deleted, or replaced,
the implementing agent:

1. Lists the affected tests in the stage 06 stop-gate report.
2. For each affected test, identifies the legacy symbol it exercises
   and the new symbol (or new behavior) it should exercise instead.
3. Surfaces this list in the stop-gate report and waits for explicit
   user approval per test before proceeding.

The agent may not silently amend or delete tests. The user reviews
each change and either approves or rejects.

### 6.2 Permitted test changes in stage 06

The following test files reference legacy `f2gh.py` symbols that
stage 06 removes. With user approval per §6.1, the implementing
agent rewrites these tests to exercise the new package modules
while preserving every observable contract:

- **`tests/test_state.py`** — `test_save_and_load_state_round_trip`
  currently monkeypatches `f2gh.STATE_FILE`. Rewrite to construct
  `StateStore(tmp_path / "state.json", source, target)` directly and
  assert the same on-disk JSON shape and load behavior.
- **`tests/test_characterization.py`** — the load/save tests
  (`test_load_state_returns_fresh_defaults_when_source_mismatches`,
  `test_load_state_returns_fresh_defaults_when_target_mismatches`,
  `test_load_state_returns_fresh_defaults_when_no_state_file`,
  `test_save_state_uses_os_replace_for_atomic_write`) become
  redundant with `tests/test_state_store.py`. **Delete them.** Keep
  the formatting and arg-parsing characterization tests unchanged.
- **`tests/test_migration_reporting.py`** — rewrite to drive
  `MigrationOrchestrator` directly with fake collaborators. The
  observable assertions (push failure non-fatal, clone failure
  terminal, per-issue failure accumulation, resume filtering)
  remain.
- **`tests/test_issue_fetch_errors.py`** — rewrite to drive
  `MigrationOrchestrator` with a fake `CodebergClient` that raises
  on `list_issues()`. The observable assertion (exit gracefully,
  no traceback) remains.
- **`tests/test_git_errors.py`** — rewrite to drive `GitMirror`
  directly with fake command runners and a fake tempdir factory. The
  observable assertions (advisory text, workflow scope detection,
  no-token-leak, push-failed-not-clone-failed) remain.
- **`tests/test_cli.py::test_main_invokes_parse_args_then_migrate`** —
  the test name references the legacy `migrate`. Rewrite to:
  - Either rename the test to
    `test_main_invokes_parse_args_then_run` and patch
    `forgejo_to_github.migration.MigrationOrchestrator.run` instead
    of `f2gh.migrate`.
  - Or, preferrably, keep the test name and have it patch the
    orchestrator's `run` method through the new public seam.
  In both cases, the observable assertion — `main()` calls
  `parse_args()` and forwards keyword arguments to the orchestrator
  — is preserved. The test function name and assertion are
  approved for renaming only via the gate in §6.1.
- **`tests/test_git_service.py::test_tag_name_containing_token_is_redacted`**
  — the new `push_tags` does not accept tag names as argv, so this
  test is **deleted**. The deletion is recorded in the stage 06
  stop-gate report.

### 6.3 Test file rewrites NOT permitted in stage 06

- **`tests/test_cli.py`** stays exactly as it is, except for the
  one test listed in §6.2.
- **`tests/test_orchestration.py`** stays exactly as it is. Its
  fake split was made in stage 04.
- **`tests/test_state_store.py`** stays exactly as it is.
- **`tests/test_codeberg_client.py`**,
  **`tests/test_github_client.py`**,
  **`tests/test_git_service.py`**,
  **`tests/test_reporting.py`** stay exactly as they are, except for
  the one test listed in §6.2.

### 6.4 CLI flag parity

The following flags must continue to be accepted by `parse_args()`
with the same help text and defaults:

| Flag | Default |
|------|---------|
| `--source OWNER/REPO` | required |
| `--target OWNER/REPO` | required |
| `--dry-run` | `False` |
| `--yes` | `False` |
| `--skip-git` | `False` |
| `--public` | `False` |
| `--description TEXT` | `None` |

`tests/test_cli.py::test_help_lists_documented_optional_flags` and
`tests/test_characterization.py::test_parse_args_*_flag` assert this
set.

## 7. Test references

CLI:

- `tests/test_cli.py::test_parse_args_missing_source_exits_nonzero_with_usage`
- `tests/test_cli.py::test_parse_args_missing_target_exits_nonzero_with_usage`
- `tests/test_cli.py::test_parse_args_missing_both_required_exits_nonzero`
- `tests/test_cli.py::test_parse_args_unknown_flag_exits_nonzero_with_usage`
- `tests/test_cli.py::test_help_exits_zero_and_mentions_source_target`
- `tests/test_cli.py::test_help_lists_documented_optional_flags`
- `tests/test_cli.py::test_main_is_callable_entrypoint`
- `tests/test_cli.py::test_main_invokes_parse_args_then_migrate` (subject to
  the rewrite in §6.2)
- `tests/test_cli.py::test_parse_args_returns_namespace_with_expected_attributes`

Characterization:

- `tests/test_characterization.py::test_parse_args_minimum_required_source_target`
- `tests/test_characterization.py::test_parse_args_dry_run_flag`
- `tests/test_characterization.py::test_parse_args_yes_flag`
- `tests/test_characterization.py::test_parse_args_skip_git_flag`
- `tests/test_characterization.py::test_parse_args_public_flag`
- `tests/test_characterization.py::test_parse_args_description_value`
- `tests/test_characterization.py::test_parse_args_all_flags_combined`
- `tests/test_characterization.py::test_format_issue_body_preserves_author_date_and_body`
- `tests/test_characterization.py::test_format_issue_body_handles_none_body`
- `tests/test_characterization.py::test_format_comment_body_preserves_author_date_and_body`
- `tests/test_characterization.py::test_format_comment_body_handles_none_body`

State round-trip via `StateStore` directly:

- `tests/test_state.py::test_save_and_load_state_round_trip` (rewritten
  per §6.1 to use `StateStore`; see §6.2)

Full suite remains green.

## 8. Implementation order

1. Add a new `f2gh._build_orchestrator(args: argparse.Namespace) ->
   MigrationOrchestrator` helper inside `f2gh.py`. It builds the
   collaborators and the `Repository`. This isolates the wiring
   logic so it can be tested directly.
2. Rewrite `f2gh.main()` to call `_build_orchestrator(args)` and then
   `orchestrator.run()`, `reporter.render_final(result)`,
   `sys.exit(reporter.exit_outcome(result))`.
3. Run `./scripts/run-tests.sh tests/test_cli.py
   tests/test_characterization.py`. Confirm green.
4. Apply the test rewrites and deletions in §6.2, with the user
   approval recorded in the stage 06 stop-gate report.
5. Remove the legacy helpers from `f2gh.py`:
   `migrate`, `mirror_git_repo`, `cb_headers`, `gh_headers`,
   `gh_request`, `fetch_codeberg_description`, `create_github_repo`,
   `fetch_all_codeberg_issues`, `fetch_codeberg_comments`,
   `create_github_issue`, `create_github_comment`,
   `close_github_issue`, `check_target_repo`, `load_state`,
   `save_state`, `get_github_token`, `codeberg_token`,
   `_ExitMessage`. Keep only `parse_args`, `main`, and the new
   `_build_orchestrator` helper.
6. Run `./scripts/run-tests.sh`. The full suite must be green.
7. Run `ruff check .` and `mypy f2gh.py forgejo_to_github/` and
   confirm both are clean.
8. Stop and report.

## 9. Verification commands

```bash
./scripts/run-tests.sh tests/test_cli.py
./scripts/run-tests.sh tests/test_characterization.py
./scripts/run-tests.sh tests/test_state.py
./scripts/run-tests.sh tests/test_migration_reporting.py
./scripts/run-tests.sh tests/test_issue_fetch_errors.py
./scripts/run-tests.sh tests/test_git_errors.py
./scripts/run-tests.sh                          # full suite
ruff check .
mypy f2gh.py forgejo_to_github/
```

`./scripts/run-tests.sh -x` may be used to stop on the first failure
for faster debugging during the rewrite.

## 10. Stop gate

The implementing agent stops and reports:

- Confirmation that `f2gh.py` exposes only `parse_args`, `main`, and
  the `_build_orchestrator` helper.
- The list of test files rewritten and the list of test functions
  deleted, with the user-approved justification for each.
- Test results for the full suite plus `ruff check .` and
  `mypy f2gh.py forgejo_to_github/`.
- The exact wording of any user-facing CLI error messages that
  changed (e.g., the missing-token message), so the user can review.
- Confirmation that `pyproject.toml` is unchanged.

The user reviews before stage 07 begins.

## 11. Out of scope

- Modifying `pyproject.toml`. The `f2gh = "f2gh:main"` entry point
  stays.
- Adding a `--state-file` CLI flag. Not in this plan.
- Adding a `--quiet` or `--json` reporter flag. Not in this plan.
- Cancellation handling. That is `plans/03-keyboard-interrupt-handling.md`.
- Clone cache retention. That is `plans/04-retain-clone-cache.md`.
- Local-clone mode. That is `plans/05-local-clone-invocation.md`.
- Replacing `argparse` with Click or another library. The CLI stays
  on `argparse`.
