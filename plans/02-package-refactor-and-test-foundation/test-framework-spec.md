# Test framework specification

**Plan:** [refactor/00-index.md](./refactor/00-index.md)
**GitHub issue:** [#3](https://github.com/zcutlip/forgejo-to-github/issues/3)

This document is the authoritative specification for the test framework that
accompanies the package refactor. It defines scope, tooling, organization,
naming, assertion style, and the rules under which tests are written, run,
modified, and treated as a locked contract. It is a specification, not an
implementation. No test code lives here; the file does not create, modify, or
delete any test modules.

The intent is to make every behavior the migration tool relies on — state
persistence, formatting, CLI surface, API contracts, Git operations,
orchestration, and reporting — independently verifiable, with no live
credentials and no network access.

## 1. Scope

### 1.1 In scope

- All behavior currently implemented in `f2gh.py` that is part of the public
  contract: CLI options, exit codes, dry-run semantics, source/target
  validation, `--skip-git`, checkpoint/resume, migration ordering, error
  classification, and final reporting.
- All new behavior introduced during the refactor: package boundaries,
  dependency-injected clients, structured failure types, and atomic state
  persistence.
- Pure functions (formatters, validators, state loaders/savers) covered by
  direct unit tests.
- I/O-bound behavior (HTTP, subprocess) covered through narrow seams that
  accept test doubles. The tests assert on observable contract: requests
  issued, payload shape, error translation, redaction, and side effects on
  injected fakes.
- Orchestration behavior: phase ordering, per-issue dependency handling,
  resumption semantics, and aggregation of per-issue results.
- Reporter behavior: progress messages, final summary, success/incomplete/
  failure outcomes, and the actionable advice emitted for known error
  classes.

### 1.2 Out of scope

- Live network access to Codeberg, GitHub, or any other remote service.
- Real subprocess execution of `git` against the host filesystem. Subprocess
  execution is covered through a boundary that is replaced by a test double
  in the test suite.
- Modifications to `.pre-commit-config.yaml`, `pyproject.toml` formatting
  configuration, or any CI/pre-commit plumbing. The test framework does not
  introduce new hooks or change existing ones.
- Performance benchmarks, load tests, or property-based fuzz tests. These
  may be added later, but are not part of this plan's completion criteria.
- Documentation beyond what is necessary to run the tests.

### 1.3 Project constraints (binding)

- Python 3.12+ as the interpreter baseline. Tests must run under any
  supported 3.12+ interpreter without conditional skips for syntax.
  This matches `pyproject.toml` (`requires-python = ">=3.12"`). The
  master plan's mention of "3.10+" is editorial drift and is
  corrected by the staged refactor spec.
- External dependencies must remain minimal. Standard library first; the
  test suite may add only widely-used, lightweight packages that are
  already permitted by the existing tooling (e.g., `pytest`, `responses`,
  `pytest-mock` if already in use).
- No live credentials, tokens, or environment-dependent behavior. The test
  suite must pass on a developer machine with no `GITHUB_TOKEN` or
  `CODEBERG_TOKEN` set.
- External API and subprocess interactions are mocked. `responses` (or an
  equivalent in-repo mock) is used for HTTP; subprocess execution is
  replaced through a test double injected at the boundary.
- The test suite is the locked contract. Tests are not modified to make an
  implementation pass; see §10 for the modification protocol.
- The user is the gatekeeper. Plan approval, RED-stage review, GREEN-stage
  review, and final review are user-held checkpoints. Automated lint or
  test results do not constitute approval.

## 2. Tooling and execution

### 2.1 pytest as the runner

`pytest` is the only test runner. Test files are collected automatically by
file naming convention. No `conftest.py`-only behavior is required to run
the suite; fixtures are local to the modules that use them or shared
explicitly.

### 2.2 Required execution wrapper

All tests must be invoked through the project's test wrapper:

```bash
./scripts/run-tests.sh [pytest args...]
```

The wrapper activates the centralized virtualenv at
`~/.virtualenvs/forgejo-to-github` and forwards all arguments to `pytest`.
The test suite must not require a project-local `.venv` and must not assume
that `pytest` is on `PATH` outside the wrapper. Individual test files may
not be invoked directly with `pytest`.

Common invocations:

- Full suite: `./scripts/run-tests.sh`
- Single file: `./scripts/run-tests.sh tests/test_state.py`
- Single test: `./scripts/run-tests.sh tests/test_state.py::test_atomic_replace`
- Pattern: `./scripts/run-tests.sh -k "redact"`
- Verbose: `./scripts/run-tests.sh -v`
- Stop on first failure: `./scripts/run-tests.sh -x`

The wrapper is the source of truth. Direct `pytest` invocations are not
used in development and are not part of the documented workflow.

### 2.3 Static checks (informational, not gating)

After test changes pass, the developer runs `ruff check .` and `mypy` on
the resulting package as a final verification. Static checks are not part
of the test framework's command surface and are not invoked from within
the suite.

## 3. Test organization

### 3.1 Directory layout

```
tests/
  __init__.py
  conftest.py                # shared fixtures (fakes, builders, factories)
  test_state.py              # state.json loading, saving, atomic replace
  test_formatting.py         # pure formatting (issue bodies, comments)
  test_cli.py                # CLI argument parsing and exit-code mapping
  test_repository_description.py
  test_codeberg_client.py    # Codeberg-side HTTP, mocked with responses
  test_github_client.py      # GitHub-side HTTP, mocked with responses
  test_git_service.py        # clone, push, tag, redaction, failure mapping
  test_orchestration.py      # phase ordering, per-issue deps, resumption
  test_reporting.py          # final summary, advice, exit-code mapping
  test_package_boundaries.py # structural rules against proxy classes
  fixtures/                  # JSON payloads, captured Git outputs, builders
```

Module names use `test_<area>.py`. File names do not encode class names;
they encode the behavior area. Multiple test classes per file are
acceptable when they cover related behavior in the same area.

### 3.2 Layout rules

- One behavior area per file. `test_state.py` does not also test CLI
  parsing.
- Helpers and builders live in `conftest.py` or under `tests/fixtures/`
  with explicit module names. Helper modules are not named like test
  modules; they never begin with `test_`.
- `__init__.py` is empty. Do not place fixtures or imports in it.
- Test files import the public package API only. Reaching into private
  names (`_*`) requires a comment justifying the need and is reviewed at
  the GREEN stage.

### 3.3 Fixture discipline

- Fixtures are narrow and named after the thing they produce: `state`,
  `fake_state_store`, `fake_codeberg`, `fake_github`, `fake_git`,
  `capsys_logger`, `runner` (for `click.testing.CliRunner` if the CLI uses
  Click, or an argparse equivalent otherwise).
- Fakes (test doubles) are first-class fixtures. They live alongside the
  tests that use them or in `conftest.py` when shared.
- No fixture composes more than three other fixtures. Deeper composition
  indicates a missing abstraction in the system under test.
- No fixture reaches into the file system beyond `tmp_path`. Tests that
  need a `state.json` use `tmp_path`; they do not write to the project
  tree.

## 4. Naming and assertion rules

### 4.1 Test names

- Test module names: `test_<area>.py` (snake_case).
- Test function names: `test_<behavior>_<condition>_<expected_outcome>`.
  Examples:
  - `test_load_state_missing_file_returns_empty_state`
  - `test_save_state_writes_atomically_via_tmp_then_replace`
  - `test_clone_failure_logs_advice_and_exits_nonzero`
  - `test_branch_push_failure_does_not_block_issue_migration`
- Class-based grouping is optional and must use `Test<Behavior>` naming
  with no `Test` prefix on helper classes.

### 4.2 Assertion style

- One logical assertion per test. Multiple `assert` statements are allowed
  only when they verify a single logical property (for example, the
  structure of a returned dict). If two distinct properties are being
  verified, the test is split.
- Prefer specific assertions. Use `assert x == y` rather than
  `assert x` when equality is intended. Use `pytest.raises` with a
  specific exception type and `match=` for messages.
- Avoid asserting on incidental formatting of error messages. Assert on
  structured fields (error code, category, advice list) and only assert
  on substrings of messages when the substring is a public contract.
- No `assert True`, no `pass`-only tests, no tests that exist only to
  demonstrate importability.

### 4.3 Comments and docstrings

- Test docstrings are short and state the contract being verified:
  "An atomic replace replaces the destination only after the temp file is
  fully written and fsynced." Avoid restating the test name.
- Comments explain non-obvious fakes or fixtures, not the obvious
  behavior.

## 5. State and checkpoint tests

The state store handles `state.json` loading, validation, checkpointing,
and atomic persistence. Tests cover:

### 5.1 Loading

- Missing file → returns a fresh empty state without raising.
- Malformed JSON → raises a structured `StateLoadError` carrying the path
  and a redaction-safe message.
- Schema mismatch (unknown key, missing required key) → raises a
  `StateLoadError` with the offending key.
- Version field handling: present and supported → load; present and
  unsupported → `StateLoadError` with an explicit "unsupported state
  version" message.
- Atomic load reads through `os.replace`-written files without partial
  reads.

### 5.2 Saving

- Writes to a temporary path in the same directory, fsyncs, then
  `os.replace`s into place. The test asserts the sequence by spying on
  the file-system calls.
- A crash between the temp write and the `os.replace` leaves no
  partial destination file. The test simulates the crash by raising
  from the spy and confirms the destination is unchanged.
- Permissions errors are translated to a structured `StateWriteError`
  and never reach the CLI as a raw `OSError`.
- The saved JSON is human-readable: indent=2, sorted keys, trailing
  newline.

### 5.3 Checkpointing

- After successfully creating an issue, the store records a checkpoint
  with the source issue number and the new GitHub issue number.
  Per-issue `closed` state and per-comment progress are **not**
  persisted in this plan; resume of a partially-completed issue
  re-creates all comments and re-issues the close. This is the
  approved simplification.
- A checkpoint never advances for a step that did not succeed.

### 5.4 Resumption

- On startup, the orchestrator reads the checkpoint and skips issues
  already migrated.
- If a checkpoint references a source issue that no longer exists on
  Codeberg, the orchestrator logs an explicit warning and skips it.
- Resumption is deterministic: running the same migration twice against
  the same state file produces identical subsequent checkpoints
  (modulo timestamps, which are injectable).

## 6. Pure formatting tests

Formatting covers Markdown blocks for issue bodies, comments, and
attribution lines. Tests are pure: inputs are dicts; outputs are strings;
no I/O is involved.

### 6.1 Attribution block

- Original author username and timestamp are rendered into a Markdown
  block at the top of an issue or comment when configured to attribute.
- The attribution block is omitted entirely when attribution is disabled.
- Timestamps are normalized to UTC and rendered in a single fixed format
  (ISO 8601 with `Z`).
- The block survives multi-line and special-character usernames
  (`@user.name`, `@user-name`, Unicode names) without breaking Markdown.

### 6.2 Issue body formatting

- Plain text bodies round-trip unchanged.
- Bodies containing Codeberg-specific references (e.g., relative links
  to other issues) are not rewritten. The test asserts that the input
  is preserved verbatim in the output.
- Empty bodies render to a sensible non-empty block so that downstream
  GitHub Markdown does not collapse the issue to an empty post.
- Bodies with embedded HTML are passed through without escaping
  (Markdown rendering is GitHub's responsibility).

### 6.3 Comment formatting

- Comments preserve their original Markdown verbatim below the
  attribution block.
- Empty comments render to a single non-empty paragraph so that the
  comment is not silently dropped.

### 6.4 Label name normalization

- Labels containing spaces are preserved; no silent renaming.
- Label color is passed through as provided; the test asserts that the
  GitHub client's payload carries the original color when known.
- Label descriptions are passed through; the test asserts they appear
  in the GitHub payload.

## 7. CLI tests

The CLI is the public entry point. Tests cover:

### 7.1 Argument parsing

- All existing flags parse: `--source`, `--target`, `--dry-run`,
  `--skip-git`, `--yes`, `--public`, `--description`, and any
  additional flags introduced during the refactor (added only with
  user approval). The previous mention of `--state-file` is
  editorial drift; that flag is not introduced in this plan. The
  `--verbose` flag is also not part of the current CLI surface and
  is not asserted here.
- Source/target accept `owner/repo` form; bare names raise a structured
  parse error.
- Conflicting flags (e.g., a future `--local-clone` and `--skip-git`)
  raise a structured parse error and exit with a documented code.

### 7.2 Help and usage

- `--help` exits 0 and lists every flag with a one-line description.
- Missing required arguments exit non-zero with a usage message on
  stderr.

### 7.3 Exit codes

- A successful migration with no issues migrated (empty Codeberg
  repository) exits 0.
- A successful migration with all issues migrated exits 0.
- A migration that completed some but not all issues (partial failure)
  exits with the documented "incomplete" code. The exact code is
  asserted; no magic numbers.
- A migration that failed before producing any issues exits with the
  documented "failure" code, distinct from the incomplete code.
- `--dry-run` exits 0 regardless of underlying state; it performs
  read-only, `GET`-only discovery and emits the dry-run preview
  summary (§7.4).

### 7.4 Dry-run semantics

- Dry run is read-only, not offline. Read-only `GET` requests against
  Codeberg and GitHub are permitted for discovery (target repository
  status, source metadata/description, source issues, and each
  discovered source issue's comments). Tests assert
  that no mutating request (`POST`/`PATCH`/`PUT`/`DELETE`) is
  registered with the mock transport.
- Dry-run invokes no git subprocess. Token reads (the environment or
  `gh auth token`) are still permitted because they are local,
  non-network reads.
- Dry-run still loads state but never writes the checkpoint file.
  Tests assert that a pre-populated destination file is byte-for-byte
  unchanged after a dry-run that exits normally, and that
  `StateStore.save` is not called during the run.
- Dry-run still validates source and target arguments.
- The dry-run result carries a populated `DryRunDiscovery` value with
  the target repository, target-repo existence, discovered comment
  count, state path, and checkpoint count. `discovery` is
  `None` on normal runs; normal-run result behavior is unchanged.
- Dry-run always exits 0 regardless of underlying state. The reporter
  emits the approved informative dry-run preview, rendered from the
  `DryRunDiscovery` value and `result.issues_discovered`. The preview
  consists of these lines:

  ```
  Dry-run complete — no changes were made.
  Target repo: owner/target
  Repo: would be created        (or `Repo: existing` when the target already exists)
  Issues: would process N issues
  Comments: would post M
  Git: clone skipped, push skipped (dry-run)
  State: path (K checkpointed)
  ```

  where `N` is the discovered issue count, `M` is
  `discovery.comments_discovered`, and the `State:` line
  shows `discovery.state_path` and
  `discovery.state_migrated`. The preview does not count
  discovery as work: `issues_attempted` stays `0` on a dry run, the
  discovered count is carried separately in `issues_discovered`, and
  the summary does not claim "migrated" or "complete" as migration
  outcomes and does not enumerate failures.

## 8. Repository-description tests

The migration optionally sets the GitHub repository description from
the Codeberg description.

- When the Codeberg description is present and non-empty, the GitHub
  repository's description is set to that value.
- When the Codeberg description is empty or missing, the GitHub
  description is left unchanged. The test asserts that no PATCH is
  issued against the repository description endpoint.
- When `--dry-run` is set, no description update is issued, but the
  would-be value is logged.
- Description updates are retried with exponential backoff on transient
  failures and translated to a structured `RepoDescriptionError` on
  terminal failure.

## 9. Codeberg API client tests

The Codeberg client wraps the Forgejo v1 API for issues, comments, and
labels. Tests use `responses` (or equivalent in-repo mock) and assert
on request shape, response handling, and error translation.

### 9.1 Listing

- Issues are listed page-by-page until exhaustion. The test asserts
  that pagination links are followed and that the final empty page
  terminates iteration.
- Closed and open issues are both returned; filtering by state is the
  caller's responsibility and is not asserted here.
- Per-issue comments are listed with the correct `issue_id` query
  parameter and follow pagination identically.

### 9.2 Single-resource fetch

- Fetching a single issue by number returns the parsed payload.
- Fetching a non-existent issue returns a structured
  `CodebergNotFoundError` carrying the issue number and the URL.
- Fetching with a 401/403 returns a structured `CodebergAuthError`.
- Fetching with a 5xx (after configured retries) returns a structured
  `CodebergTransientError`.

### 9.3 Payload shape

- The client sends `Authorization: token <CODEBERG_TOKEN>` only when a
  token is configured. The test asserts that the header is absent when
  no token is set, and present (and redaction-safe in error paths)
  when set.
- The client sends `Accept: application/json` and sets a descriptive
  `User-Agent`.

### 9.4 Error translation

- Transport errors (connection refused, DNS failure) translate to
  `CodebergTransportError` and do not leak token values.
- Rate-limit responses (HTTP 429) translate to
  `CodebergRateLimitError` and include a `retry_after` field when the
  header is present.

## 10. GitHub API client tests

The GitHub client wraps the GitHub REST API v3 for issue creation,
comment creation, label management, and repository description. Tests
mirror the Codeberg client tests in style.

### 10.1 Issue creation

- A POST to `/repos/{owner}/{repo}/issues` carries the expected
  payload: title, body, labels (if any), and state.
- The response's `number` field is parsed and returned.
- A 422 (validation) translates to `GitHubValidationError` carrying
  the parsed error messages.
- A 401/403 translates to `GitHubAuthError`.

### 10.2 Comment creation

- A POST to `/repos/{owner}/{repo}/issues/{number}/comments` carries
  the formatted comment body.
- The response's `id` field is parsed and returned.

### 10.3 Labels

- POST to create labels sends `name`, `color`, and `description`.
- Existing labels are detected via GET and not recreated. The test
  asserts that no second POST is issued for an already-present label.
- Labels missing color fall back to a documented default color. The
  default value is asserted.

### 10.4 Secondary rate limiting

- A 403 with `X-RateLimit-Remaining: 0` translates to
  `GitHubRateLimitError` carrying the reset timestamp.
- The client honors `Retry-After` on 429 and secondary-rate-limit
  responses.
- After three consecutive secondary-rate-limit responses, the client
  raises `GitHubRateLimitError` rather than retrying forever.

### 10.5 Repository description

- PATCH to `/repos/{owner}/{repo}` with `description` set is issued
  only when a non-empty description is provided.
- The PATCH is not issued when the description is empty/missing.
- The PATCH is not issued in `--dry-run` mode.

### 10.6 Redaction

- Every error path that could include the GitHub token (request
  headers, response bodies, exception messages) is asserted to be free
  of the token substring. The test uses a recognizable sentinel token
  and searches the captured error text for it.

## 11. Git mirror tests

The Git service handles clone, branch push, tag push, redaction, and
failure classification. Tests use a fake subprocess boundary; no `git`
binary is invoked.

### 11.1 Clone

- A successful clone returns the local path and records the command
  (`git clone <url> <path>`) on the fake.
- A clone that exits non-zero (simulated by the fake returning a
  non-zero exit) raises a structured `GitCloneError` carrying the exit
  code, stderr, and a redacted command line.
- A clone that exits 128 with "Authentication failed" in stderr is
  classified as `GitAuthError` (a subclass of `GitCloneError`) with
  actionable advice "token lacks repository access".
- A clone that times out (the fake raises a `TimeoutExpired`) is
  classified as `GitCloneTimeoutError`.
- A clone whose stderr contains the token is asserted to be redacted
  in every error path (the test scans the captured exception text for
  the sentinel token).

### 11.2 Branch push

- A successful branch push returns the remote ref recorded on the fake.
- A push that exits non-zero raises `GitPushError`; the test asserts
  that the orchestrator's reaction is "log and continue with issue
  migration" — i.e., the push failure does not raise out of the
  orchestrator.
- A push rejected as "non-fast-forward" is classified as
  `GitPushRejectedError` with advice "force push or pull first".

### 11.3 Tag push

- A successful tag push returns the list of pushed refs.
- A tag push failure raises `GitTagPushError`. Tag push failures are
  terminal for the Git phase but do not block issue migration.
- Tags whose names contain the token substring are redacted in any
  logged command line.

### 11.4 Redaction discipline

- Every command line logged or attached to an exception is run through
  a single redaction function. The test asserts that the function is
  invoked on every log line emitted by the Git service.
- The redaction function replaces tokens in URLs (`https://x-access-token:TOKEN@host/...`)
  and in command arguments (`-c http.extraHeader=Authorization: token TOKEN`)
  with a stable placeholder.

### 11.5 Workflow advice

- A clone failure emits an advice block containing: (a) the most
  likely cause, (b) a concrete remediation step, (c) a link or
  pointer to the relevant Codeberg/GitHub docs when applicable. The
  test asserts the presence and order of these three pieces.
- A tag push failure emits an advice block referencing the tag name
  and the retry strategy.
- A non-fast-forward push failure emits an advice block recommending
  `git pull --rebase` or `--force-with-lease`.

### 11.6 Terminal clone vs non-fatal push

- A clone failure is terminal: the orchestrator does not proceed to
  issue migration. The test asserts that no GitHub API request is
  registered after a failed clone.
- A branch push failure is non-fatal: the orchestrator logs the
  failure, continues to issue migration, and includes the failure in
  the final report. The test asserts that GitHub API requests are
  registered after a failed branch push.
- A tag push failure is non-fatal for the same reasons as branch
  push.

## 12. Orchestration and per-issue dependency tests

The orchestrator coordinates phases and per-issue substeps. Tests inject
fake clients and a fake state store, then assert on the sequence and
outcomes.

### 12.1 Phase ordering

- Repository description update runs before issue migration.
- Label creation runs before the first issue that uses those labels.
- Per-issue substeps run in order: create issue, post first comment,
  post remaining comments, advance checkpoint.
- The orchestrator does not call the GitHub client for issue creation
  until the GitHub repository is verified to exist. The test asserts
  that a "repo not found" check runs first.

### 12.2 Per-issue dependency

- Within one issue, comment posting depends on issue creation.
- Across issues, comment posting for issue N does not depend on issue
  N+1.
- A failure in issue creation prevents checkpoint advancement and
  marks the issue as failed; subsequent issues are still attempted.
- A failure in a non-first comment does not roll back earlier
  comments; the checkpoint advances to the last successful comment
  index.

### 12.3 Resumption semantics

- A migration that resumes from a checkpoint skips issues already
  marked complete.
- A migration that resumes from a checkpoint retries only the
  comments that were not yet posted.
- A migration that resumes after a partial failure re-enters the
  issue at the first non-checkpointed substep.

### 12.4 Aggregation

- The final result aggregates: number of issues migrated, number of
  issues failed, number of comments posted, number of comments failed,
  list of failures with their structured codes.
- The aggregation is deterministic given fixed inputs and injectable
  timestamps.

## 13. Error and reporting tests

The reporter produces human-readable progress, the final summary, and
actionable advice. Tests capture stdout/stderr through the standard
pytest `capsys` fixture and assert on substrings that are part of the
documented contract.

### 13.1 Progress

- "Starting migration of N issues" appears on stdout when migration
  begins.
- "Created issue #N on GitHub" appears after each successful issue
  creation.
- "Migrated N/M issues" appears at the end of each issue.

### 13.2 Final summary

- The final summary includes: total issues, migrated, failed, total
  comments, posted, failed, and the elapsed time (which is
  injectable).
- The final summary is written to the normal-output sink on success
  and to the error-output sink on any failure. The reporter's
  dual-sink design (one for normal output, one for error output)
  is exercised in `tests/test_reporting.py::test_reporter_writes_failure_summary_to_error_sink`.

### 13.3 Exit-code mapping

- All-issues-migrated → exit 0.
- Some-issues-migrated, some-failed → exit code for "incomplete".
- No-issues-migrated, fatal early failure → exit code for "failure".
- The exact codes are asserted; they are public contract.

### 13.4 Truthfulness

- The summary never claims an issue was migrated when it was not.
  The test injects a failure and asserts the summary does not
  contain "migrated" for the failed issue.
- The summary never under-counts failures. The test injects N
  failures and asserts the failure count is exactly N.
- The summary never leaks the GitHub or Codeberg token. The test
  scans the captured output for the sentinel token and asserts it is
  absent.

## 14. Package-boundary tests

These tests enforce structural rules about the package design. They
exist to prevent the refactor from producing proxy classes that wrap a
single unrelated function, mechanical extraction that adds no
abstraction, and other anti-patterns that dilute the design. They run
as ordinary pytest tests; they inspect the package's public surface
through import and introspection (no AST parsing beyond what `inspect`
provides).

### 14.1 No proxy classes

- A class is rejected if it has exactly one public method that simply
  delegates to a function or another class's method. The test asserts
  that no public class in the package exposes a single non-special
  method. Special methods (`__init__`, `__enter__`, `__exit__`,
  `__repr__`, etc.) are excluded.
- A class is rejected if it exposes only attributes that forward to
  another object's attributes with no added behavior. The test
  asserts no such "attribute shuttle" exists.

### 14.2 Meaningful OO design

- Every public class has a docstring stating its responsibility in
  one sentence. The test asserts each class has a non-empty
  docstring.
- Every public class has at least two non-special public methods OR
  one public method plus one or more meaningful attributes used in
  tests. The test asserts this minimum.
- Every public class is referenced by at least one test file. The
  test asserts each class appears as an attribute or constructor
  argument in some test module. Orphan classes are flagged.

### 14.3 Dependency injection

- The GitHub client, Codeberg client, Git service, state store, and
  reporter are all instantiable with explicit constructor arguments
  for their dependencies. The test instantiates each with a fake
  dependency and asserts construction succeeds.
- No class performs network, subprocess, or filesystem I/O at module
  import time. Imports of `requests`, `subprocess`, `os`, `pathlib`,
  and similar modules are permitted for later use, but the import
  statement must not trigger any side effect. Such side effects must
  be encapsulated behind an injected seam. The test asserts that
  monkey-patching `requests.post` does not change the behavior of
  an injected client, and that importing the package does not
  perform any I/O.

### 14.4 Module boundaries

- The CLI module imports the orchestrator but not the GitHub client
  directly. The test asserts the import graph.
- The orchestrator imports the API clients and Git service but does
  not import `requests` or `subprocess` directly.
- The reporter may import `sys` for the default sinks; it does not
  import `requests`, `subprocess`, or `argparse`. It consumes
  already-formatted strings from the orchestrator.

### 14.5 No god object

- No single class has more than seven public methods (excluding
  special methods). The test asserts the count for every public
  class. Classes that exceed the threshold must be split; the test
  flags the violation.

### 14.6 No silent extraction

- Refactoring commits that simply move code from `f2gh.py` into a
  new module without changing structure or adding tests are rejected
  at review. This rule is enforced socially and by the test suite's
  coverage: every behavior moved out of `f2gh.py` must be covered
  by a corresponding test in this plan's test framework.

## 15. RED-stage rules

The RED stage is the contract. Tests must fail meaningfully before
implementation begins, and the failure must point at the missing
behavior, not at the test infrastructure.

### 15.1 RED must be real

- Every test fails before implementation, with an assertion error or
  an expected exception that points at the missing contract. A test
  that fails because of an `ImportError` for a not-yet-existing
  symbol is acceptable only when the symbol is the contract being
  established; otherwise the test must be reworked to import the
  existing surface.
- Tests are not skipped or xfailed at the RED stage. Skipped tests do
  not establish a contract.

### 15.2 RED classification

Each test is classified at creation time. The classification is
recorded in a comment in the test file header (`# RED class: <class>`)
and is one of:

- **A. Pure unit** — no I/O, no mocks beyond the system under test.
  Examples: state load/save, formatting, validation. Always required
  to fail before implementation.
- **B. Boundary unit** — uses a single seam fake (HTTP or subprocess)
  to exercise one boundary. Always required to fail before
  implementation.
- **C. Integration** — exercises the orchestrator with multiple seam
  fakes together. Required to fail before implementation, but with a
  relaxed failure message: a `NotImplementedError` raised from the
  orchestrator's stub is acceptable as long as the test asserts the
  stub's behavior, not its absence.
- **D. Structural** — enforces package boundaries. May be added at
  any stage but is expected to be added before the refactor
  completes. These tests may pass against the current `f2gh.py`
  without modification; their value is in catching regressions
  during the refactor.

### 15.3 RED review gate

After RED is established, the developer stops and reports:

- The list of test files and the count of tests in each.
- The classification (A/B/C/D) of each test.
- The observed failure messages for a representative sample.
- Any test that did not fail and the reason.

The user reviews and either approves the RED stage or requests
amendments. GREEN does not begin until the RED stage is approved.

### 15.4 RED contract gaps

If the RED stage exposes a legitimate gap in the documented contract
— for example, a behavior the original `f2gh.py` has but this
specification does not cover — the developer stops and surfaces the
gap. With user approval, the test is amended to cover the gap, and
the developer stops again for user approval of the amended test
before resuming GREEN. Tests are never amended solely to make an
implementation pass.

## 16. Test modification and re-lock rules

The test suite is a locked contract once the RED stage is approved.

### 16.1 When a test may be amended

A test may be amended only when:

- The amendment closes a legitimate contract gap surfaced during RED
  (§15.4) and the user has approved the amendment.
- The amendment fixes a typo, import, or fixture error that does not
  change the contract. Such a fix must be made before the GREEN
  stage begins and is part of the RED review.
- The original `f2gh.py` is found to behave differently from what
  the test asserts, and the user approves aligning the test with
  reality (with a note explaining the deviation).

### 16.2 When a test may not be amended

A test may not be amended to:

- Make an implementation pass when the implementation does not
  satisfy the original contract.
- Weaken an assertion to accommodate an implementation shortcut.
- Skip or xfail a failing test without user approval.
- Rename the public API the test exercises, unless the rename is
  itself part of the approved refactor.

### 16.3 Re-lock protocol

When the GREEN stage is complete, the developer proposes a "re-lock"
of the test suite: a final commit (or commit series) in which the
tests are confirmed unchanged from the RED-approved baseline except
for approved amendments. The re-lock is the user's final review of
the test framework.

## 17. Refactor sequencing after RED

After RED is approved, the developer proceeds in this order. Each
step ends at a stable checkpoint where the tests pass and the
package boundaries are intact.

1. **Extract pure/domain code.** Move formatting, validation, error
   and result models, and state persistence. No I/O changes. Tests
   in `test_formatting.py` and `test_state.py` should pass against
   the extracted code.
2. **Extract Codeberg and GitHub clients.** Keep transport behind
   injected seams. Tests in `test_codeberg_client.py` and
   `test_github_client.py` should pass.
3. **Extract Git service.** Keep subprocess behind an injected seam.
   Tests in `test_git_service.py` should pass.
4. **Extract orchestrator and reporter.** Wire the orchestrator to
   the extracted clients and services. Tests in
   `test_orchestration.py` and `test_reporting.py` should pass.
5. **Wire CLI to the orchestrator.** Tests in `test_cli.py` should
   pass. The CLI imports the orchestrator only.
6. **Run package-boundary tests.** Tests in
   `test_package_boundaries.py` should pass and should not have been
   relaxed to accommodate the refactor.
7. **Run the full suite.** All tests pass; `ruff check .` and
   `mypy` are clean.

At each step, if a test that was passing at RED begins to fail, the
developer stops and reports. The test is not amended to make the
implementation pass.

## 18. Completion criteria

This plan is complete when:

- All test files listed in §3 exist and run under
  `./scripts/run-tests.sh` with no warnings about collection.
- All RED-classified tests fail meaningfully before implementation
  (verified at the RED review gate).
- All tests pass after the refactor, with the implementation
  satisfying the contracts the tests assert.
- `test_package_boundaries.py` passes and would fail if any of the
  anti-patterns in §14 were introduced.
- No test requires live network access, live credentials, or a real
  `git` subprocess.
- The full verification suite is documented in §2 and matches what
  the wrapper actually does.
- The user has approved the RED stage, the GREEN stage, and the
  re-lock of the test suite.
- The refactor preserves the public CLI surface and migration
  semantics; deviations are documented and approved.
- The pre-commit configuration is unchanged.
- Subsequent plans (03–05) can build on the new package boundaries
  without returning logic to a monolithic script.

## 19. References

- Plan index: [refactor/00-index.md](./refactor/00-index.md)
- Issue: [#3](https://github.com/zcutlip/forgejo-to-github/issues/3)
- Predecessor plan: [../01-clone-failure-followup.md](../01-clone-failure-followup.md)
- Project guidelines: [../../AGENTS.md](../../AGENTS.md)
- Test wrapper: `scripts/run-tests.sh`
- Tooling: `pytest`, `responses` (or equivalent), centralized virtualenv
  at `~/.virtualenvs/forgejo-to-github`
