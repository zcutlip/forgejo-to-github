# Stage 07 — Completion and cleanup

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** stages 01–06.
**Blocks:** nothing; this is the final stage.

## 1. Objective

Verify that the refactor meets every documented completion criterion
from the master plan and the test framework spec. Move the active
plan into `plans/archive/`. Prepare the closing comment for the GitHub
issue that tracks this plan. This stage does not modify code; it
verifies, records, and archives.

## 2. Files / modules

This stage writes to:

- `plans/02-package-refactor-and-test-foundation/02-package-refactor-and-test-foundation.md` — left in place (not modified).
- `plans/02-package-refactor-and-test-foundation/test-framework-spec.md` — left in place.
- `plans/02-package-refactor-and-test-foundation/refactor/00-index.md` through `07-completion.md` — left in place for historical reference.
- `plans/archive/02-package-refactor-and-test-foundation.md` — the master plan is moved here.
- `plans/archive/02-package-refactor-and-test-foundation/` — the supporting directory (containing `test-framework-spec.md` and `refactor/`) is moved here.

This stage does not modify source code.

## 3. Completion criteria — verification matrix

Each row is a check. The implementing agent (or the user, at review)
runs the check and records the result. Every check must pass before
the plan can be closed.

| # | Criterion | How to verify | Source |
|---|-----------|---------------|--------|
| 1 | Full test suite passes | `./scripts/run-tests.sh` exits 0 | master plan §Completion criteria; test framework spec §18 |
| 2 | No test requires live network access | grep test files for `requests.` and `urllib.`; assert only `responses` / `FakeTransport` / mocks appear; `tests/test_package_boundaries.py::test_importing_package_does_not_perform_network_calls` and `tests/test_package_boundaries.py::test_importing_package_does_not_execute_subprocess` pass | test framework spec §1.2, §18 |
| 3 | No test requires real credentials | No test reads `GITHUB_TOKEN` or `CODEBERG_TOKEN` from the environment | master plan §Completion criteria |
| 4 | `ruff check .` is clean | `ruff check .` exits 0 | master plan §Completion criteria |
| 5 | `mypy f2gh.py forgejo_to_github/` is clean | `mypy f2gh.py forgejo_to_github/` exits 0 (or reports no errors on the listed paths) | master plan §Completion criteria |
| 6 | Public CLI surface preserved | `./scripts/run-tests.sh tests/test_cli.py tests/test_characterization.py` exits 0 | master plan §Completion criteria |
| 7 | Each public class has a docstring | `tests/test_package_boundaries.py::test_public_class_has_docstring` passes for all six classes | test framework spec §14.2 |
| 8 | Each public class has 2–7 public methods | `tests/test_package_boundaries.py::test_public_class_has_at_least_two_public_methods` and `test_public_class_has_at_most_seven_public_methods` pass | test framework spec §14.5 |
| 9 | No proxy classes | Manual inspection of the diff; no class has a single public method that simply delegates; `forgejo_to_github.formatting` remains functions, not a class | test framework spec §14.1 |
| 10 | Migration semantics preserved (clone terminal, push non-fatal) | `tests/test_orchestration.py::test_clone_failure_is_terminal_and_skips_issue_migration`, `tests/test_orchestration.py::test_push_failure_does_not_block_issue_migration`, `tests/test_migration_reporting.py::test_git_push_failure_is_non_fatal_and_reported`, `tests/test_git_service.py::test_clone_failure_is_terminal_no_github_api_call_after` all pass | master plan §Goals; test framework spec §11.6 |
| 11 | Per-issue substep ordering preserved | `tests/test_orchestration.py::test_create_issue_runs_before_comments_and_checkpoint` passes | test framework spec §12.1, §12.2 |
| 12 | Resume semantics preserved | `tests/test_migration_reporting.py::test_successful_issues_are_checkpointed_and_resume_filters_them` passes | test framework spec §12.3 |
| 13 | Truthful reporting preserved | `tests/test_reporting.py::test_result_with_failure_does_not_claim_all_migrated`, `tests/test_migration_reporting.py::test_issue_failure_is_accumulated_and_later_issues_continue` pass | test framework spec §13.4 |
| 14 | Token redaction preserved at every boundary | `tests/test_codeberg_client.py::test_transport_error_does_not_leak_token`, `tests/test_git_service.py::test_clone_stderr_token_is_redacted_in_error_text`, `tests/test_git_service.py::test_url_token_is_redacted_in_logged_command`, `tests/test_git_service.py::test_extra_header_token_is_redacted_in_command`, `tests/test_git_service.py::test_tag_name_containing_token_is_redacted` all pass | master plan §Design constraints; test framework spec §10.6, §11.4 |
| 15 | Atomic state writes preserved | `tests/test_state_store.py::test_save_calls_os_replace_for_atomic_write`, `test_save_persists_all_fields_and_uses_atomic_replace` pass | master plan §Goals; test framework spec §5.2 |
| 16 | `--skip-git`, `--dry-run`, source/target flags preserved | `tests/test_characterization.py::test_parse_args_*_flag` and `tests/test_cli.py::test_help_lists_documented_optional_flags` pass | master plan §Design constraints |
| 17 | Pre-commit configuration unchanged | `git diff .pre-commit-config.yaml` is empty; the file has not been modified during the refactor | master plan §Design constraints |
| 18 | `f2gh` console-script entry point still resolves | `python -c "import f2gh; assert callable(f2gh.main)"` exits 0; `pyproject.toml` is unchanged | master plan §Work sequence |
| 19 | Orchestrator constructs no collaborators of its own | `tests/test_orchestration.py::test_orchestrator_constructor_accepts_injected_dependencies` passes; `forgejo_to_github/migration.py` does not call `CodebergClient(...)`, `GitHubClient(...)`, `GitMirror(...)`, `StateStore(...)`, or `Reporter(...)` constructors | stage 04 invariants; binding decisions |
| 20 | Domain types are plain dataclasses, not pydantic | `forgejo_to_github/migration.py` and `forgejo_to_github/state.py` import `dataclasses`, not `pydantic`; `MigrationState` and `MigrationResult` are `@dataclass` | binding decisions |

## 4. Issue closure protocol

When every row in §3 passes, the implementing agent (or the user,
at their option) prepares the closing comment on
[issue #3](https://github.com/zcutlip/forgejo-to-github/issues/3). The
comment template is:

```markdown
Closing this issue: the package refactor and test foundation from
`plans/02-package-refactor-and-test-foundation/` is complete.

Verification:
- `./scripts/run-tests.sh` — passes
- `ruff check .` — clean
- `mypy f2gh.py forgejo_to_github/` — clean

Commits:
- <commit-sha-1>: <subject>
- <commit-sha-2>: <subject>
- ...

The plan and its supporting documents have been moved to
`plans/archive/02-package-refactor-and-test-foundation/`.

Subsequent plans (03 keyboard-interrupt handling, 04 clone cache
retention, 05 local-clone invocation) can build on the new package
boundaries.
```

The agent does **not** post this comment autonomously. The user posts
the closing comment, or explicitly asks the agent to post it.

## 5. Plan archival

Move the entire plan directory to `plans/archive/`:

```bash
mkdir -p plans/archive
git mv plans/02-package-refactor-and-test-foundation plans/archive/02-package-refactor-and-test-foundation
```

The relative links in `00-index.md` point to the master plan and test
framework spec by relative path. After the move, those links still
resolve because the supporting directory moves together with the
master plan.

The user performs the archive move, or explicitly asks the agent to
perform it.

## 6. Out of scope for this stage

- Implementing any further functionality. This stage verifies and
  records only.
- Modifying `f2gh.py` or any package module.
- Modifying `pyproject.toml`, `.pre-commit-config.yaml`, `setup.cfg`,
  `README.md`, or any CI configuration.
- Adding new tests.
- Writing the implementation for any future plan.
- Posting the GitHub issue closure comment without explicit user
  direction.

## 7. What this plan does not deliver

The following items remain for subsequent plans and are explicitly
**not** in scope for this plan or this directory:

- Cancellation handling on Ctrl-C (`plans/03-keyboard-interrupt-handling.md`).
- Clone cache retention between runs
  (`plans/04-retain-clone-cache.md`).
- Local-clone mode where the user pre-clones the repository
  (`plans/05-local-clone-invocation.md`).

Each of those plans builds on the package boundaries established
here. None of them requires changes to `f2gh.py`'s public surface;
they add new options or new behavior inside the existing seams.

## 8. Stop gate

Stage 07 closes with:

1. The verification matrix in §3 fully checked, with the commands
   run and the outcomes recorded.
2. The plan directory archived per §5.
3. The closing comment drafted per §4 (not posted unless the user
   directs).
4. A final summary from the implementing agent describing what
   changed, what tests cover the change, and any deviations from the
   spec — even wording changes that the user approved during stages
   04–06.

The user reviews the closing summary and either approves or requests
amendments. Approval here is the lock that ends this plan.
