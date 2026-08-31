# Staged refactor specification — index

**Test framework spec:** [`../test-framework-spec.md`](../test-framework-spec.md)
**GitHub issue:** [#3](https://github.com/zcutlip/forgejo-to-github/issues/3)
**Predecessor plan:** [../../01-clone-failure-followup.md](../../01-clone-failure-followup.md)
**Predecessor issue:** [#1](https://github.com/zcutlip/forgejo-to-github/issues/1)

This directory is the authoritative, staged implementation specification for
the package refactor. It does not contain implementation, and it does not
contain test code. Each numbered file below describes one stage of the
refactor in enough detail that a follow-up agent can execute it without
re-deriving intent.

## Purpose

Refactor the monolithic `f2gh.py` script into a small, multi-file Python
package with clear object-oriented boundaries and a proper automated test
suite. The refactor preserves the existing CLI behavior and migration
semantics while making failure handling, state transitions, and future
features easier to reason about and test.

This plan follows `plans/01-clone-failure-followup.md`. Its core clone-failure
fix is already complete; the remaining verification work from plan 01 is
captured by the tests added here.

## High-level scope

The package owns the following responsibilities, with explicit seams between
them:

- **CLI** — argument parsing, command entry point, and exit-code translation.
- **Configuration/domain models** — validated source/target repositories,
  migration options, migration results, and structured failures.
- **Codeberg client** — repository metadata, issues, and comments.
- **GitHub client** — repository creation and issue/comment/label
  operations.
- **Git mirror service** — clone, branch/tag pushes, cleanup, redaction,
  and Git-specific failure classification.
- **State store** — loading, validating, checkpointing, and atomically
  saving `state.json`.
- **Migration orchestrator** — phase ordering, per-issue dependency
  handling, resumption, and aggregation of results.
- **Reporter** — human-readable progress, final status, actionable advice,
  consistent success/incomplete/failure outcomes, and dual-sink routing
  (normal output to `sys.stdout`, failure output to `sys.stderr`).

Dependency injection is used for network clients, Git command execution,
state storage, and output. Tests must run without network access or real
credentials.

## High-level goals

- Preserve the `f2gh` console entry point and existing command-line
  behavior. The current CLI surface (`--source`, `--target`, `--dry-run`,
  `--yes`, `--skip-git`, `--public`, `--description`) is preserved
  exactly. No `--state-file` flag is introduced in this plan.
- Establish focused module boundaries instead of growing `f2gh.py`
  further.
- Apply sound object-oriented design without creating a god object or
  needless abstraction. Classes have one clear responsibility and depend
  on interfaces/protocols where that improves testability.
- Make API, Git, state, orchestration, and reporting behavior
  independently testable.
- Keep external dependencies minimal. The interpreter baseline is Python
  3.12+, matching `pyproject.toml` (`requires-python = ">=3.12"`).
- Keep state human-readable and atomically written with `os.replace`.

## Dependencies

- Follows `plans/01-clone-failure-followup.md`.
- Must precede keyboard-interrupt handling (`plans/03-...`), clone-cache
  retention (`plans/04-...`), and local-clone optimization
  (`plans/05-...`) because those features cross orchestration, Git,
  state, cleanup, and reporting boundaries.

## High-level completion criteria

- The package has clear ownership of CLI, API, Git, state,
  orchestration, and reporting responsibilities.
- The `f2gh` entry point works without behavior regressions.
- Tests cover both successful and partial-failure paths, including
  clone/push errors and stateful resume.
- No test requires live credentials or network access.
- `./scripts/run-tests.sh`, `ruff check .`, and
  `mypy f2gh.py forgejo_to_github/` pass.
- Plans 03–05 can build on the new boundaries without returning logic
  to a monolithic script.

The detailed verification matrix is defined in
[`07-completion.md`](./07-completion.md).

## Authoritative spec

The files in this directory are the single source of truth for
module-level decisions: constructor signatures, method semantics,
error hierarchies, dependency-injection rules, and stage ordering.
This index carries the binding decisions; the stage documents carry
the per-stage details. Conflicts between this directory and any
parent or sibling plan are resolved in favor of this directory.

## Binding decisions (set during prior conversation and review pass)

These decisions are locked and not negotiable inside any individual stage.
If a stage appears to require deviating from one of them, stop and surface
the conflict rather than improvising.

- **Python baseline: 3.12+.** Matches `pyproject.toml` (`requires-python =
  ">=3.12"`). The master plan and README text that say "3.10+" are
  editorial drift and are corrected in this plan. No 3.10/3.11
  compatibility shims.

- **Domain types: plain `@dataclass`es.** No `pydantic`, no `attrs`. Use
  `dataclasses.dataclass(frozen=True)` for value objects and a plain
  `@dataclass` for the orchestrator's `MigrationResult`. The dataclasses
  live in `forgejo_to_github.domain` (or a similarly-named module) and
  are imported across stages. No "Formatter" class will exist; pure
  formatting functions continue to live in `forgejo_to_github.formatting`.

- **HTTP transport injection.** `CodebergClient` and `GitHubClient`
  accept a `Transport` Protocol. The default factory returns an adapter
  built on `requests.Session`. Tests pass a fake transport. No class
  imports `requests` at module scope to perform side effects. The
  `Transport` Protocol is synchronous; the production adapter is
  `requests`-based. Async is out of scope.

- **Git injection.** `GitMirror` accepts a `command_runner` callable
  (matching the `subprocess.run` shape used by the existing code), a
  `tempdir_factory` callable (matching `tempfile.mkdtemp`'s shape), a
  `cleanup` callable (matching `shutil.rmtree`'s shape), and the
  GitHub token used to construct the push URL. Tests pass fakes for
  every one. The default `command_runner` is a thin wrapper around
  `subprocess.run` (with `check=True`, `capture_output=True`,
  `text=True`) constructed at instance time. The default `tempdir_factory`
  is `tempfile.mkdtemp` constructed at instance time. The default
  `cleanup` is `shutil.rmtree` constructed at instance time. None of
  these defaults is constructed at module import time.

- **Reporter Sink Protocol.** `Reporter` accepts two `Sink` Protocols
  (one for normal output, one for error output). Each sink has a
  `write(line: str) -> None` method. The default normal-output sink
  wraps `sys.stdout`; the default error-output sink wraps `sys.stderr`.
  Tests pass recording sinks. The reporter decides which sink to write
  to based on the event type; the CLI does not select channels.

- **Orchestrator constructor injection.** `MigrationOrchestrator.__init__`
  takes exactly **five** collaborators and one configuration value by
  name. The locked signature is:

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

  The five collaborators are `codeberg`, `github`, `git`, `state`,
  `reporter`. `repo` is an immutable value object (a frozen
  `Repository` dataclass) that the CLI constructs from
  `argparse.Namespace`. The orchestrator does not instantiate any
  collaborator. No factory method on the orchestrator, no builder, no
  proxy class wrapping a single function.

  The orchestrator is responsible for ordering phases and aggregating
  results. The CLI is responsible for constructing collaborators and
  translating the result into a process exit code. The orchestrator
  never calls `sys.exit` and never raises `SystemExit`; it returns a
  `MigrationResult` from `run()`.

- **Final summary ownership.** The orchestrator returns the
  `MigrationResult`. The CLI calls `reporter.render_final(result)`. The
  orchestrator does not call `render_final`. This is the single, locked
  source of final-summary emission. The legacy inline `print` block at
  the bottom of `f2gh.migrate` is removed in stage 06.

- **User-facing wording.** Minor wording changes in progress lines,
  summary headers, and advisory text are permitted provided observable
  meaning and approved test contracts are preserved. Substantive meaning
  changes (e.g., "Migration complete" appearing on a failure run) are
  not permitted and must be raised for approval.

- **No test weakening.** The test suite is the locked contract per
  `test-framework-spec.md` §16. Tests may not be amended to make
  implementation pass without explicit user approval and a recorded
  reason. The protocol for amending or deleting a test during the
  refactor is defined in `06-cli-wiring.md` §6 and is invoked only with
  user approval at the stage where the amendment is needed.

- **Dry-run contract.** When `repo.dry_run is True`:

  - No HTTP request is issued against Codeberg or GitHub. Tests assert
    no request is registered with the mock transport.
  - No subprocess is invoked for `git clone`, `git push`, or `gh auth
    token`. The Git phase is recorded as `skipped`. Token reads
    (including the `gh auth token` fallback for `GITHUB_TOKEN`) are
    **still** performed because they are local, non-network reads from
    the environment or `gh`'s local auth state.
  - State is loaded but never written. The destination `state.json`
    must be unchanged after a dry-run that exits normally.
  - Input validation (parse_args, source/target shape) runs.
  - The orchestrator produces a `MigrationResult` whose `git["clone"]`
    and `git["push"]` are both `"skipped"`, whose failure lists are
    empty, and whose counters are zero. The reporter's final summary
    is the dry-run summary (no `Migrated` claims). Exit code is
    `EXIT_SUCCESS` (0) regardless of underlying state.

- **Checkpoint schema.** A checkpoint is one entry per Codeberg issue.
  Each entry is an `IssueCheckpoint` dataclass:

  ```python
  @dataclass(frozen=True)
  class IssueCheckpoint:
      source_number: int
      github_number: int
      state: str
      closed: bool
  ```

  `MigrationState.migrated: dict[int, int]` is preserved in name for
  backward compatibility with the legacy on-disk format but the new
  in-memory `MigrationState` is `dict[int, IssueCheckpoint]`. The
  on-disk format remains `{"source": ..., "target": ..., "migrated":
  {"<src>": <gh>}}` where the value is the GitHub issue number.
  Per-comment progress is not persisted; the GitHub API does not
  provide per-issue atomicity, and plan 02 does not introduce one.
  Resume of a partially-completed issue re-runs all comments and
  re-issues the close. (See `01-state-store.md` §3.1 for the schema
  rationale and the user approval recorded for this simplification.)

- **Pre-commit configuration.** `.pre-commit-config.yaml`,
  `pyproject.toml`, and any CI configuration are not modified by this
  plan. Any change requires explicit user approval.

- **State file path.** The state file path is supplied by the CLI.
  The default is `Path("state.json")` in the current working directory.
  No `--state-file` flag is introduced in this plan. The test framework
  spec's mention of `--state-file` is editorial drift and is removed
  from §3.1 and §7.1.

- **Status values for `MigrationResult.git`.** Allowed values are
  `"ok"`, `"failed"`, and `"skipped"`. `"skipped"` means the phase was
  not attempted (either `--skip-git` or `--dry-run`).

## Dependency order

The eight stages are ordered strictly by dependency. Earlier stages
define the seams (Protocols, default factories, domain types) that later
stages consume.

1. **`01-state-store.md`** — `StateStore`, `MigrationState` and
   `IssueCheckpoint` dataclasses, `StateLoadError`, `StateWriteError`,
   atomic write helper, schema validation. No I/O collaborators; pure
   module except for filesystem access via `os.replace`.
2. **`02-api-clients.md`** — `CodebergClient`, `GitHubClient`,
   `Transport` Protocol, default requests adapter, error types,
   redaction behavior. Consumes nothing from later stages.
3. **`03-git-mirror.md`** — `GitMirror`, command runner / tempdir
   factory / cleanup injection, GitHub token ownership, error types,
   advisory classification, redaction helper.
4. **`04-orchestrator.md`** — `MigrationOrchestrator` constructor
   injection of the five collaborators, `Repository` and
   `MigrationResult` dataclasses, dry-run override, per-issue state
   machine, aggregation logic, checkpoint advancement. Consumes
   `StateStore`, `CodebergClient`, `GitHubClient`, `GitMirror`,
   `Reporter`. Does **not** call `reporter.render_final`.
5. **`05-reporter.md`** — `Reporter`, `Sink` Protocol, default stdout
   and stderr sinks, truthful final summary, exit-outcome helper.
   Consumes `MigrationResult`.
6. **`06-cli-wiring.md`** — arg-parsing preservation, collaborator
   construction, exit-code translation, `--state-file` decision.
   Consumes all five stages above. **Includes the test-rewrite
   protocol that allows legacy test files to be amended under
   user-supervised rules.**
7. **`07-completion.md`** — full verification matrix, plan/issue
   closure criteria, archive procedure.

## Ground rules for every stage

- **No implementation during spec review.** The files in this directory
  are the specification. After they are written, no code changes are
  made until the user reviews and approves the spec. Implementation
  happens stage-by-stage, with each stage stopping for review.
- **Each stage stops for review.** After completing a stage, the
  implementing agent reports the diff summary, the test reference list,
  and the verification commands run. The user reviews before the next
  stage begins.
- **Test names are referenced by name, not invented.** Where a test
  already exists in the repository, the spec cites the existing
  function name and module path. Where a test does not yet exist but
  is implied by the test-framework spec, the spec labels it `to be
  added` rather than fabricating a function name.
- **No proxy classes, no formatter classes.** A class that wraps a
  single function is rejected. Formatting remains a function module.
  Orchestration helpers that exist purely to forward arguments are
  rejected.
- **Collaborator ownership is explicit.** Only the CLI constructs
  concrete collaborators. Production entry points and tests both go
  through the CLI seam (production) or direct constructor injection
  (tests).

## Traceability table

This table maps each stage to the test modules and test functions it
must satisfy. Existing tests are referenced by their actual names;
future tests are labeled `to be added`.

| Stage | Primary module(s) | Test module(s) | Test functions (existing) | Test functions (to be added) |
|-------|-------------------|----------------|---------------------------|------------------------------|
| 01-state-store | `forgejo_to_github.state` | `tests/test_state_store.py` | `test_load_returns_default_state_when_file_absent`, `test_load_ignores_checkpoint_with_mismatched_source`, `test_load_ignores_checkpoint_with_mismatched_target`, `test_save_persists_all_fields_and_uses_atomic_replace`, `test_save_calls_os_replace_for_atomic_write`, `test_round_trip_restores_int_keys`, `test_state_store_does_not_expose_module_level_state_file`, `test_state_store_uses_instance_path_not_module_global`, `test_state_store_constructor_takes_path_source_target`, `test_save_signature_locked` | — |
| 01-state-store (legacy compatibility) | `forgejo_to_github.state` | `tests/test_characterization.py` | `test_load_state_returns_fresh_defaults_when_source_mismatches`, `test_load_state_returns_fresh_defaults_when_target_mismatches`, `test_load_state_returns_fresh_defaults_when_no_state_file`, `test_save_state_uses_os_replace_for_atomic_write` | — |
| 01-state-store (orchestrator seam) | `forgejo_to_github.migration` | `tests/test_orchestration.py` | `test_create_issue_runs_before_comments_and_checkpoint` (asserts checkpoint via injected fake) | `to be added`: `test_per_issue_checkpoint_advances_only_on_full_success` |
| 02-api-clients | `forgejo_to_github.codeberg` | `tests/test_codeberg_client.py` | `test_list_issues_paginates_until_empty_page`, `test_list_issues_sends_expected_request_params`, `test_list_issues_sets_json_accept_and_user_agent`, `test_list_issues_omits_auth_header_when_no_token`, `test_list_issues_sends_token_authorization_when_configured`, `test_list_comments_passes_issue_id_param_and_paginates`, `test_get_issue_returns_parsed_payload`, `test_get_issue_404_raises_not_found_with_context`, `test_get_issue_auth_errors_raise_codeberg_auth_error`, `test_transport_error_translates_to_codeberg_transport_error`, `test_transport_error_does_not_leak_token`, `test_429_translates_to_rate_limit_error_with_retry_after`, `test_429_without_retry_after_header_still_raises_rate_limit_error` | — |
| 02-api-clients | `forgejo_to_github.github` | `tests/test_github_client.py` | `test_create_repository_private_posts_expected_payload`, `test_create_repository_public_posts_private_false`, `test_create_repository_includes_description_when_provided`, `test_create_issue_posts_expected_payload_and_returns_number`, `test_create_comment_posts_body_and_returns_id`, `test_close_issue_patches_state_closed`, `test_ensure_label_posts_payload_when_label_missing`, `test_ensure_label_does_not_repost_when_label_already_exists`, `test_create_issue_422_raises_validation_error_with_messages`, `test_create_issue_auth_errors_raise_github_auth_error`, `test_403_with_zero_rate_limit_remaining_raises_rate_limit_error`, `test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error` | — |
| 02-api-clients (description behavior) | `forgejo_to_github.codeberg` and `forgejo_to_github.github` | `tests/test_repository_description.py` | (legacy `f2gh.migrate` tests; rewritten in stage 06 to drive `MigrationOrchestrator` directly; the four end-state contracts remain: explicit description wins; non-empty source description is forwarded; empty source description is not forwarded as a PATCH; HTTP failure on the source metadata call does not forward a PATCH.) | — |
| 03-git-mirror | `forgejo_to_github.git` | `tests/test_git_service.py` | `test_clone_success_returns_local_path_and_records_command`, `test_clone_nonzero_exit_raises_structured_git_clone_error`, `test_clone_auth_failure_is_classified_as_git_auth_error`, `test_clone_timeout_classified_as_git_clone_timeout_error`, `test_clone_stderr_token_is_redacted_in_error_text`, `test_branch_push_success_returns_remote_ref`, `test_branch_push_failure_raises_git_push_error`, `test_branch_push_non_fast_forward_is_classified_with_advice`, `test_tag_push_success_returns_pushed_refs`, `test_tag_push_failure_raises_git_tag_push_error`, `test_tag_name_containing_token_is_redacted`, `test_url_token_is_redacted_in_logged_command`, `test_extra_header_token_is_redacted_in_command`, `test_clone_failure_advice_has_cause_remediation_and_docs_pointer`, `test_tag_push_failure_advice_references_tag_and_retry`, `test_non_fast_forward_advice_recommends_rebase_or_force_with_lease`, `test_clone_failure_is_terminal_no_github_api_call_after`, `test_branch_push_failure_is_nonfatal_does_not_abort`, `test_tag_push_failure_is_nonfatal_for_issue_migration` | — |
| 03-git-mirror (legacy `mirror_git_repo` parity, exercised via `f2gh.py`) | `f2gh.mirror_git_repo` | `tests/test_git_errors.py` | `test_clone_network_failure_exits_with_advisory_and_no_token_leak`, `test_clone_auth_failure_mentions_codeberg_token`, `test_push_workflow_scope_rejection_emits_workflow_advisory`, `test_generic_push_failure_labeled_git_push_failed_not_clone_failed` | — |
| 03-git-mirror (orchestrator hookup) | `forgejo_to_github.migration` | `tests/test_orchestration.py` | `test_clone_runs_before_any_issue_work`, `test_clone_failure_is_terminal_and_skips_issue_migration`, `test_push_failure_does_not_block_issue_migration` | — |
| 04-orchestrator | `forgejo_to_github.migration` | `tests/test_orchestration.py` | all functions in `tests/test_orchestration.py` | `to be added`: `test_per_issue_checkpoint_advances_only_on_full_success`, `to be added`: `test_resume_skips_issues_already_in_state`, `to be added`: `test_result_aggregates_counts_for_reporter`, `to be added`: `test_dry_run_makes_no_http_or_subprocess_calls`, `to be added`: `test_dry_run_does_not_write_state` |
| 04-orchestrator (truthful reporting surfaces) | `forgejo_to_github.migration` | `tests/test_migration_reporting.py` | `test_git_push_failure_is_non_fatal_and_reported`, `test_clone_failure_is_terminal_and_skips_issue_fetch`, `test_issue_failure_is_accumulated_and_later_issues_continue`, `test_successful_issues_are_checkpointed_and_resume_filters_them` | — |
| 04-orchestrator (issue fetch error handling) | `forgejo_to_github.migration` | `tests/test_issue_fetch_errors.py` | `test_migrate_source_404_exits_gracefully` | — |
| 04-orchestrator (combined GitHub/Codeberg API seam coverage as legacy baseline) | `f2gh` module-level functions | `tests/test_api_clients.py` | all functions in `tests/test_api_clients.py` | — |
| 05-reporter | `forgejo_to_github.reporting` | `tests/test_reporting.py` | `test_reporter_constructor_accepts_injected_output_sink`, `test_complete_result_reports_complete_migration`, `test_result_with_failure_does_not_claim_all_migrated`, `test_report_names_every_failure_exactly_once`, `test_git_push_failure_summary_does_not_replay_multiline_advisory`, `test_clone_failure_summary_marks_clone_status_distinctly` | `to be added`: `test_reporter_writes_failure_summary_to_error_sink` |
| 05-reporter (truthfulness / partial-failure reporting at the migrate seam, which funnels into Reporter) | `f2gh.migrate` | `tests/test_migration_reporting.py` | `test_issue_failure_is_accumulated_and_later_issues_continue`, `test_git_push_failure_is_non_fatal_and_reported` | — |
| 06-cli-wiring | `f2gh.parse_args`, `f2gh.main` | `tests/test_cli.py` | `test_parse_args_missing_source_exits_nonzero_with_usage`, `test_parse_args_missing_target_exits_nonzero_with_usage`, `test_parse_args_missing_both_required_exits_nonzero`, `test_parse_args_unknown_flag_exits_nonzero_with_usage`, `test_help_exits_zero_and_mentions_source_target`, `test_help_lists_documented_optional_flags`, `test_main_is_callable_entrypoint`, `test_main_invokes_parse_args_then_migrate`, `test_parse_args_returns_namespace_with_expected_attributes` | — |
| 06-cli-wiring (preserve old CLI flag behavior) | `f2gh.parse_args` | `tests/test_characterization.py` | `test_parse_args_minimum_required_source_target`, `test_parse_args_dry_run_flag`, `test_parse_args_yes_flag`, `test_parse_args_skip_git_flag`, `test_parse_args_public_flag`, `test_parse_args_description_value`, `test_parse_args_all_flags_combined` | — |
| 06-cli-wiring (state round-trip via CLI path preserved) | `StateStore` constructed in test | `tests/test_state.py` | `test_save_and_load_state_round_trip` (rewritten per `06-cli-wiring.md` §6.1 to use `StateStore(path, source, target)` directly) | — |
| 06-cli-wiring (legacy `migrate` happy-path surface) | `f2gh.migrate` | `tests/test_migration_reporting.py` | all functions (rewritten in stage 06 per §6.1 to drive `MigrationOrchestrator` directly) | — |
| 07-completion | n/a (process) | all of the above | all of the above | — |

## Stop gate

After every stage, the implementing agent must stop and report. The agent
does not begin the next stage without explicit user approval. The report
must include:

1. A list of files added/modified.
2. The verification commands run and their outcomes.
3. Any test functions whose names did not match what this spec asserted
   and what adjustment was made (the agent must surface rather than
   silently rename).
4. Any deviation from this spec, even minor wording changes, with a
   justification.

## Out of scope for this spec directory

- Implementation of any stage.
- Test code. Tests are referenced by name; they are not edited from this
  directory.
- Modifications to `pyproject.toml`, `setup.cfg`, `.pre-commit-config.yaml`,
  or any CI configuration.
- Changes to `f2gh.py` beyond what stage 06 explicitly authorizes.
- Subsequent plans (`03-keyboard-interrupt-handling.md`,
  `04-retain-clone-cache.md`, `05-local-clone-invocation.md`). Those
  plans will reference this directory but do not block it.
