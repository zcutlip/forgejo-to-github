# Stage 03 — GitMirror

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** stage 01 (no direct dependency on state, but stage 04
needs both state and git to coexist).
**Blocks:** stage 04 (orchestrator), stage 06 (CLI wiring).

## 1. Objective

Replace the monolithic `mirror_git_repo(source, target, dry_run)`
function in `f2gh.py` with a `GitMirror` class whose boundaries are
injectable. The class must:

- Accept a `command_runner` callable matching the `subprocess.run`
  shape used by the existing code (`(args, *, check, capture_output,
  text, **kwargs) -> CompletedProcess`-shaped object, or raise
  `subprocess.CalledProcessError` on failure).
- Accept a `tempdir_factory` callable matching `tempfile.mkdtemp`'s
  shape (`(*, prefix) -> str`).
- Accept a `cleanup` callable matching `shutil.rmtree`'s shape
  (`(path, ignore_errors=True) -> None`).
- Accept the GitHub token used to construct the authenticated push
  URL.
- Perform clone, branch push, and tag push as separate public methods.
- Translate failures into structured exceptions:
  `GitCloneError`, `GitAuthError`, `GitCloneTimeoutError`,
  `GitPushError`, `GitPushRejectedError`, `GitTagPushError`.
- Attach a multi-line advisory block to each failure with cause,
  remediation, and a docs pointer (where applicable).
- Redact tokens in every command line surfaced through a log line or an
  exception.

The class must not invoke any subprocess or touch the filesystem other
than through the injected `command_runner`, `tempdir_factory`, and
`cleanup` callables.

## 2. Files / modules

- **New module:** `forgejo_to_github/git.py` containing:
  - `GitMirror` class.
  - Error hierarchy: `GitError` base + the six subclasses above.
  - Module-private `redact_token(value: str, token: str | None) -> str`
    helper.

- `forgejo_to_github/__init__.py` is not modified during this stage.

## 3. Public API and responsibilities

### 3.1 `GitMirror`

Constructor signature:

```python
GitMirror(
    source_url: str,
    target_url: str,
    github_token: str,
    command_runner: Callable[..., Any] | None = None,
    tempdir_factory: Callable[..., str] | None = None,
    cleanup: Callable[..., None] | None = None,
)
```

Defaults:

- `command_runner` defaults to a thin wrapper around `subprocess.run`
  that returns the `CompletedProcess` and re-raises
  `CalledProcessError` on non-zero exit (matching the legacy call
  sites: `check=True`, `capture_output=True`, `text=True`). The
  default is constructed at instance time, not at module import time.
- `tempdir_factory` defaults to `tempfile.mkdtemp` constructed at
  instance time. The factory's `prefix` argument is set to
  `f"f2gh-{repo_name}-"` (where `repo_name` is the second component of
  the target slug, e.g. `widgets` from `owner/widgets`).
- `cleanup` defaults to `shutil.rmtree` constructed at instance time
  with `ignore_errors=True` bound.

`github_token` is **required** (no default). The token is used to
construct the authenticated push URL
(`https://x-access-token:{token}@{host}/...`) inside `push_branches`
and `push_tags`. The constructor does not retain the bare `target_url`
plus token as separate state; it stores them and assembles the
authenticated URL at push time.

The default constructor signature is **required** to be keyword-friendly
for `command_runner=`, `tempdir_factory=`, and `cleanup=` so tests can
override them by name. The test fixtures in `tests/test_git_service.py`
rely on passing these by keyword:

```python
GitMirror(
    source_url="...",
    target_url="...",
    github_token="sentinel",
    command_runner=runner,
    tempdir_factory=fs_factory,
    cleanup=cleanup_recording,
)
```

### 3.2 Methods

| Method | Returns | Behavior |
|--------|---------|----------|
| `clone() -> str` | local path (the tempdir created by the factory) | Runs `git clone --mirror <source_url> <tmpdir>`. On `CalledProcessError`, classifies into `GitCloneError` (default), `GitAuthError` (auth-related stderr), or `GitCloneTimeoutError` (`subprocess.TimeoutExpired`). On `TimeoutExpired`, raises `GitCloneTimeoutError`. On success, returns the tempdir path string. The tempdir is **not** cleaned up here; cleanup is the caller's responsibility (via the cleanup contract below). |
| `push_branches(local_path: str) -> None` | None | Runs `git -C <local_path> push <auth_url> --all` to push all branches in one command. The auth URL is constructed as `https://x-access-token:{token}@{host}/...`. On `CalledProcessError`, classifies into `GitPushError` (default) or `GitPushRejectedError` (non-fast-forward stderr). The redaction routine is applied to the auth URL before it appears in any log line. |
| `push_tags(local_path: str) -> None` | None | Runs `git -C <local_path> push <auth_url> --tags` to push all tags in one command. On `CalledProcessError`, raises `GitTagPushError`. Tag names are not passed as argv, so there is no tag-name redaction; the redaction routine is still applied to the auth URL. |
| `cleanup(local_path: str) -> None` | None | Removes the tempdir using the injected `cleanup` callable. Idempotent; safe to call when the directory does not exist. The orchestrator (stage 04) calls this after success or after a non-fatal failure. The legacy function called `shutil.rmtree` in a `finally` block; the new orchestrator owns that lifecycle. |

The push methods do **not** take a `token` parameter; the token is
owned by the `GitMirror` instance. This eliminates the orchestrator's
need to pass a token through and resolves the "where does the token
live" contradiction in earlier drafts.

`push_branches` failure does **not** prevent `push_tags` from being
called; the orchestrator decides whether to attempt tags after a
branch push failure. The legacy behavior is to continue with tags
even when `--all` fails. The orchestrator's contract (stage 04)
preserves this.

### 3.3 Error hierarchy

```
GitError(Exception)
├── GitCloneError
│   ├── GitAuthError              # subclass of GitCloneError
│   └── GitCloneTimeoutError      # subclass of GitCloneError
├── GitPushError
│   └── GitPushRejectedError      # subclass of GitPushError
└── GitTagPushError               # separate root for clarity
```

The test asserts use class-name substring matching rather than strict
type equality:

- `test_clone_nonzero_exit_raises_structured_git_clone_error` asserts
  `"GitCloneError" in cls_name or isinstance(exc, SystemExit)`. The
  implementation must raise a `GitCloneError` (or subclass). It must
  not raise `SystemExit` directly; that wrapper belongs to the CLI.
- `test_clone_auth_failure_is_classified_as_git_auth_error` asserts
  `"GitAuthError" in cls_name or "GitCloneError" in cls_name`. The
  `GitAuthError` subclass satisfies both substrings.
- `test_branch_push_failure_raises_git_push_error` asserts
  `"GitPushError" in cls_name`. The implementation must raise a
  `GitPushError` (not a subclass — but `GitPushRejectedError` also
  satisfies the substring match, which is acceptable).
- `test_tag_push_failure_raises_git_tag_push_error` asserts
  `"GitTagPushError" in cls_name`.
- `test_branch_push_failure_is_nonfatal_does_not_abort` asserts the
  exception is *not* `SystemExit` and *not* a `GitCloneError`. The
  `GitPushError` hierarchy satisfies this.
- `test_tag_push_failure_is_nonfatal_for_issue_migration` is
  symmetrical.

### 3.4 Redaction

A module-private helper:

```python
def redact_token(value: str, token: str | None) -> str: ...
```

Behavior:

- If `token` is `None` or empty, return `value` unchanged.
- Otherwise, replace every occurrence of `token` with the literal
  string `"<REDACTED>"`.
- The substitution is global (`str.replace`), not regex.

Every command line that the class constructs — including the `auth_url`
passed to `git push` — is run through this helper before it is logged
or attached to an exception. The test
`test_url_token_is_redacted_in_logged_command` asserts that log records
emitted during `push_branches` do not contain the sentinel token.
The test `test_extra_header_token_is_redacted_in_command` asserts
the same for an `argv` whose `http.extraHeader=Authorization: token ...`
contains the token (i.e., when git itself echoes the configured
header into an error message). The test
`test_clone_stderr_token_is_redacted_in_error_text` covers the case
where the token leaks through git's stderr.

Because `push_tags` does not take tag names as argv, there is no
separate `test_tag_name_containing_token_is_redacted` requirement for
this plan. The test is removed from the contract, and the
implementation does not need to redact tag names. The
`tests/test_git_service.py::test_tag_name_containing_token_is_redacted`
test is removed in stage 06 per the test-rewrite protocol in
`06-cli-wiring.md` §6.

### 3.5 Advisory classification

Every failure exception carries an advisory `str` block with three
ordered parts:

1. **Most likely cause** — one short sentence naming the underlying
   condition (e.g., "network DNS failure", "GitHub rejected the push:
   non-fast-forward", "GitHub rejected the push: workflow scope").
2. **Concrete remediation** — a one- or two-step action the operator
   can take (e.g., "check your network connection and retry",
   "run `git pull --rebase` and retry, or push with `--force-with-lease`",
   "refresh your token with `gh auth refresh -h github.com -s workflow`").
3. **Docs pointer** — either a URL or the literal substring `docs`.

The tests assert both presence and order:

- `test_clone_failure_advice_has_cause_remediation_and_docs_pointer`
  asserts that the cause substring appears before the remediation
  substring, which appears before the docs substring.
- `test_tag_push_failure_advice_references_tag_and_retry` asserts the
  tag name and a "retry" / "again" verb are present. Because the
  push method does not accept tag names, the advisory refers to
  "tag push" generically; the assertion is updated in stage 06 to
  assert the substring `"tag push"` and either `"retry"` or
  `"again"`.
- `test_non_fast_forward_advice_recommends_rebase_or_force_with_lease`
  asserts that both `git pull --rebase` (or `rebase`) and
  `--force-with-lease` appear.

The advisory is part of the exception's `str()`. It is **not** logged
by `GitMirror` directly; logging is the orchestrator's concern (stage
04).

## 4. Invariants

- **No real subprocess at import time.** The module-level import must
  not spawn anything. The default `command_runner` is constructed
  inside `__init__`.
- **No real filesystem write outside the injected factories.** `clone()`
  does not call `tempfile.mkdtemp` directly when a `tempdir_factory`
  is supplied; it delegates to the factory. `cleanup()` does not call
  `shutil.rmtree` directly when a `cleanup` callable is supplied; it
  delegates to the callable. The defaults are `tempfile.mkdtemp` and
  `shutil.rmtree` respectively.
- **Token never appears in `str(exception)` or in any log record.**
  This invariant is asserted by `test_clone_stderr_token_is_redacted_in_error_text`,
  `test_url_token_is_redacted_in_logged_command`, and
  `test_extra_header_token_is_redacted_in_command`.
- **Clone failure is terminal.** After a `clone()` raises, no
  `push_branches` or `push_tags` call is permitted (the caller should
  not call them). The test
  `test_clone_failure_is_terminal_no_github_api_call_after` asserts
  this at the `GitMirror` boundary: no `push`-shaped call appears on
  the runner after `clone()` raises.
- **No I/O at module import.** The module is permitted to import
  `subprocess`, `tempfile`, and `shutil` for later use, but no
  subprocess is spawned and no file or directory is created at
  import time. The defaults are constructed inside `__init__`.

## 5. Collaborator / dependency rules

- `GitMirror` accepts `command_runner`, `tempdir_factory`, `cleanup`,
  and `github_token`. It does not accept a state store, a reporter,
  or API clients.
- `GitMirror` does not import from `forgejo_to_github.codeberg`,
  `forgejo_to_github.github`, `forgejo_to_github.state`, or
  `forgejo_to_github.reporting`.
- The module imports `subprocess`, `tempfile`, and `shutil` for
  runtime use; the default factories are constructed inside
  `__init__` so the package can be imported without subprocess
  capabilities or filesystem access.

## 6. Migration / compatibility constraints

- **`f2gh.mirror_git_repo` stays in place during stage 03.** The
  legacy function is the harness for the existing
  `tests/test_git_errors.py` tests. Removal happens in stage 06.
- **No change to existing CLI flags.** `--skip-git` continues to mean
  "skip the Git phase entirely" at the orchestrator level.
- **Public method signatures are part of the contract.** Tests use
  `mirror.clone()`, `mirror.push_branches(local_path)`, and
  `mirror.push_tags(local_path)`. Renames require user approval.

## 7. Test references

- `tests/test_git_service.py::test_clone_success_returns_local_path_and_records_command`
- `tests/test_git_service.py::test_clone_nonzero_exit_raises_structured_git_clone_error`
- `tests/test_git_service.py::test_clone_auth_failure_is_classified_as_git_auth_error`
- `tests/test_git_service.py::test_clone_timeout_classified_as_git_clone_timeout_error`
- `tests/test_git_service.py::test_clone_stderr_token_is_redacted_in_error_text`
- `tests/test_git_service.py::test_branch_push_success_returns_remote_ref`
- `tests/test_git_service.py::test_branch_push_failure_raises_git_push_error`
- `tests/test_git_service.py::test_branch_push_non_fast_forward_is_classified_with_advice`
- `tests/test_git_service.py::test_tag_push_success_returns_pushed_refs`
- `tests/test_git_service.py::test_tag_push_failure_raises_git_tag_push_error`
- `tests/test_git_service.py::test_url_token_is_redacted_in_logged_command`
- `tests/test_git_service.py::test_extra_header_token_is_redacted_in_command`
- `to be added`: `test_every_git_command_line_passes_through_redaction` —
  a property-style test asserting that any command line emitted by
  `GitMirror` is passed through the redaction helper.
- `tests/test_git_service.py::test_clone_failure_advice_has_cause_remediation_and_docs_pointer`
- `tests/test_git_service.py::test_tag_push_failure_advice_references_tag_and_retry`
- `tests/test_git_service.py::test_non_fast_forward_advice_recommends_rebase_or_force_with_lease`
- `tests/test_git_service.py::test_clone_failure_is_terminal_no_github_api_call_after`
- `tests/test_git_service.py::test_branch_push_failure_is_nonfatal_does_not_abort`
- `tests/test_git_service.py::test_tag_push_failure_is_nonfatal_for_issue_migration`

Removed in stage 06 (per `06-cli-wiring.md` §6 test-rewrite protocol):
- `tests/test_git_service.py::test_tag_name_containing_token_is_redacted`
  — the new `push_tags` does not accept tag names as argv, so the
  test is not applicable.

Package boundary:

- `tests/test_package_boundaries.py::test_intended_public_class_is_importable`
  (parameterized for `forgejo_to_github.git.GitMirror`)
- `tests/test_package_boundaries.py::test_public_class_has_docstring`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_least_two_public_methods`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_most_seven_public_methods`
  (same)

Legacy parity (must remain green throughout this stage):

- `tests/test_git_errors.py::test_clone_network_failure_exits_with_advisory_and_no_token_leak`
- `tests/test_git_errors.py::test_clone_auth_failure_mentions_codeberg_token`
- `tests/test_git_errors.py::test_push_workflow_scope_rejection_emits_workflow_advisory`
- `tests/test_git_errors.py::test_generic_push_failure_labeled_git_push_failed_not_clone_failed`

## 8. Implementation order

1. Add `forgejo_to_github/git.py` with `GitMirror`, the error
   hierarchy, and the redaction helper.
2. Run `./scripts/run-tests.sh tests/test_git_service.py
   tests/test_package_boundaries.py`. Confirm green.
3. Run the legacy parity tests:
   `./scripts/run-tests.sh tests/test_git_errors.py`. Confirm green.
4. Run the full suite via `./scripts/run-tests.sh`. All pre-existing
   tests must remain green.
5. Stop and report.

## 9. Verification commands

```bash
./scripts/run-tests.sh tests/test_git_service.py
./scripts/run-tests.sh tests/test_git_errors.py
./scripts/run-tests.sh tests/test_package_boundaries.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/git.py
```

`mypy forgejo_to_github/git.py` is informational.

## 10. Stop gate

The implementing agent stops and reports:

- Confirmation that `forgejo_to_github.git.GitMirror` exists and meets
  the public surface in this spec.
- Test results for the targeted suites plus the full suite.
- Any deviation from the locked method signatures or the locked error
  hierarchy, with justification.
- Confirmation that no `f2gh.py` symbols were modified in this stage.

The user reviews before stage 04 begins.

## 11. Out of scope

- Editing `f2gh.py` to call `GitMirror`. That is stage 06.
- The orchestrator. That is stage 04.
- Adding SSH-URL support. The class only handles HTTPS URLs with
  embedded tokens.
- A local-clone mode where the user pre-clones the repo. That is
  `plans/05-local-clone-invocation.md`, which builds on this stage.
- Cloning with an existing keychain / ssh-agent integration. Out of
  scope for this plan; orthogonal to the seams here.
- Persisting tag names in redaction. The push methods push all tags
  in one command, so tag names do not appear in argv.
