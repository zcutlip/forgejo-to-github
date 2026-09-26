# Test audit remediation

**GitHub issue:** none — closing work for the current branch, no new issue filed.
**Branch:** `dev/6-local-clone-invocation` (current; no new branch).
**Status:** stage 1 approved and committed. RED not yet started.
**Source:** `plans/10-test-audit-2026-09-26.md` (the audit report, left unmodified).

## Context

The test-audit report at `plans/10-test-audit-2026-09-26.md` surveyed the suite
added for the clone-cache / cwd-invocation work and reported 7 findings: 2
critical, 3 medium, 2 minor. All 7 are test-strength issues — assertions that
can pass while the guarded behavior is broken — not production defects.

This plan remediates all 7. It is the closing step of the current branch: the
point is to confirm that the tests added for this feature are sound and have
not drifted, not to add behavior.

## The governing rule: a failure is the finding

**This work has no RED stage in the usual sense, and every amended assertion is
expected to pass on contact with the current tree.**

All seven remediations are test-only. The production behavior each one guards
has already been verified correct in the current source (see "Verified
production state" below). So an honest, strengthened assertion passes
immediately. That inverts the normal gate:

> A failing amended test is **not** a test bug to be resolved by loosening the
> new assertion. It is evidence that a vacuous test was masking a live
> production defect. Surface it and stop. That finding is what makes GREEN real
> for this effort — and fixing it still requires its own approval.

Do not weaken a strengthened assertion to reach green. The only acceptable
response to a failure is to report it.

## Verified production state

Checked in the current tree so this does not get re-derived during the work:

| Behavior guarded | Location | Already correct? |
| --- | --- | --- |
| `save()` durability ordering | `forgejo_to_github/state.py:533-535` | Yes — file `fsync` → `os.replace` → `_fsync_directory` |
| Clone-failure cleanup | `forgejo_to_github/git.py:557-559` | Yes — one `finally: self.cleanup(local_path)` |
| Failed-count label | `forgejo_to_github/reporting.py:262` | Yes — emits `"... {n} failed"` |
| Ahead-branch exclusion | `forgejo_to_github/cwd_source.py:286` | Yes — `ahead` is built by iterating `remote_heads`, so a local-only ref cannot appear in it |

## Report provenance

`plans/10-test-audit-2026-09-26.md` is the audit record and is **not** edited by
this plan. Three of its claims are known to be inaccurate; they are corrected
here for planning purposes so the remediation order is right, and so the
discrepancy is recorded rather than lost.

- **Finding 2 is not CRITICAL.** It reports the three clone-cleanup tests at
  `tests/test_git_service.py:993-1068` as asserting cleanup of a path that was
  never created. True. But the cleanup behavior is already covered against a
  real partial path at `tests/test_clone_cache.py:227` (failure) and `:243`
  (interrupt) — the report's own recommendation names "the cache test" as the
  model to copy. So this is redundancy debt, not a coverage hole. Remediated as
  Group D.
- **Finding 5 is miscategorized.** The report groups all 7 findings as
  "tests that can pass while the guarded behavior is broken". Under a
  privileged runner, `chmod 0o500` does not prevent the write, so
  `pytest.raises(StateWriteError)` **fails**. That is a false-failure
  portability flake, not a vacuous pass. The remediation is unchanged.
- **Finding 3 cites the wrong lines for half its scope.** It attributes
  `tests/test_orchestration.py:336-355` to an incomparable-index comparison; that
  test performs no such comparison. Its real weakness is different — see
  Group C. Both halves are remediated.

One defect the report missed entirely is folded into Group A below.

## Remediation groups

Groups are independent. They touch disjoint test files except where noted, and
may be applied in any order.

### Group A — finding 1: import-side-effect tests never import anything

`tests/test_package_boundaries.py:92-133`. Two tests, and **two** defects.

**A1 — the import cache makes both tests vacuous.** Both call
`importlib.import_module(PACKAGE_NAME)` after the package is already in
`sys.modules`. `test_git_service.py:67` imports it at module level, and there is
no `conftest.py` in `tests/`, so nothing clears the cache. The comment at
`:107-108` — *"`importlib.import_module` will re-run the package __init__"* — is
false, and is the likely reason this went unnoticed. **Delete that comment.**

Fix: run the import in a fresh child process.

**A2 — a fresh import would still be vacuous (missed by the report).**
`forgejo_to_github/__init__.py` is three lines and imports only
`__about__.__version__`. A child process that blocks sockets and then imports
the package name would load none of `state`, `codeberg`, `github`, `git`,
`migration`, or `reporting` — the only modules that could plausibly have
import-time side effects. **Without this half, the fix is cosmetic.**

Fix: the child must also walk and import every submodule.

Concretely, both tests (`test_importing_package_does_not_perform_network_calls`,
`test_importing_package_does_not_execute_subprocess`) keep their names and their
two-test shape, and each:

1. Builds a child script that installs its guard **first** — socket-layer for
   the network test (`create_connection`, `getaddrinfo`), `subprocess.Popen` for
   the subprocess test — then imports the package, walks
   `pkgutil.walk_packages(pkg.__path__, pkg.__name__ + ".")` importing each
   submodule, then prints a sentinel.
2. Runs it with `sys.executable -c <script>`, with `PYTHONPATH` set to the
   repository root **derived from the test file's location**, not the process
   cwd — the test must not depend on an editable install being present.
3. Asserts `returncode == 0` and the sentinel appears in stdout, surfacing the
   child's stderr in the failure message. A tripped guard becomes a non-zero
   exit rather than an in-process exception, so the sentinel is what makes the
   pass meaningful.

### Group B — four independent assertion fixes

- **B1 — finding 4, `tests/test_state_store.py:361-380`.** The test patches
  `os.replace` out of existence and never records it, so the rename-vs-
  directory-fsync ordering is unpinned. Replace the `fsync_targets` list with a
  single ordered log covering both operations: `os.replace` gets a recorder side
  effect appending `"replace"`; `os.fsync` appends `"fsync:file"` or
  `"fsync:dir"` by the existing `stat.S_ISDIR` test. Assert
  `order.index("replace") < order.index("fsync:dir")`, and keep both existing
  assertions (first fsync is not a directory; exactly one directory fsync).
  `os` and `pytest` are already imported in this file.
- **B2 — finding 5, `tests/test_state_store.py:271-281`.** Add
  `@pytest.mark.skipif(os.geteuid() == 0, reason=...)`; keep the existing
  `try/finally` chmod restore. `os` and `pytest` are already imported.
  `geteuid` is POSIX-only, which is not a new constraint — `state.py:493`
  already documents that directory fsync is POSIX-only.
- **B3 — finding 6, `tests/test_reporting.py:176`.** Replace `assert "1" in text`
  with `assert "1 failed" in text.lower()`, matching the labelled-count pattern
  already used 30 lines below at `:208`. The label is produced at
  `reporting.py:262`.
- **B4 — finding 7, `tests/test_cwd_freshness.py:514-516`.** Replace
  `result.ahead == [] or any("secret-branch" not in entry for entry in result.ahead)`
  with `assert not any("secret-branch" in entry for entry in result.ahead)`. The
  current form passes whenever any *other* entry exists in `ahead`. The
  sibling presence checks at `:507-508` use the correct polarity and stay.

### Group C — finding 3: incomparable indices in the orchestration suite

`tests/test_orchestration.py`. Clone, API, and state events are recorded in
three independent lists, so positions in two of them cannot establish relative
order.

**C1 — add a shared timeline, additively.** Introduce a timeline list built in
`_build` (`:278`) and handed to `_FakeApi`, `_FakeGit`, and `_FakeState`, each
of which records its events onto it. Constructor arguments take an optional
timeline defaulting to a fresh list, so tests that construct a fake directly
  keep working. The existing `api.calls` / `state.events` lists are **retained** —
  more than 20 call sites across the file read them, and replacing them is churn
  with no correctness gain. The timeline is a new seam, not a migration.

Two details that matter:

- `_FakeState.__init__` (`:199-200`) **prepopulates** `events` with resume
  checkpoints. Those are seeded state, not ordered occurrences, and must not
  reach the timeline or the resume tests get phantom ordering.
- Three tests append to `api.calls` directly (`:1745`, `:1876`, `:1907`).
  Route these through a recording method on the fake so they land on the
  timeline too — otherwise the timeline has silent holes.

**C2 — fix `test_create_issue_runs_before_comments_and_checkpoint` (`:427-475`).**
The assertion `create_index < issue1_checkpoint` compares an index into
`api.calls` with an index into `state.events`. Convert it to a
same-timeline comparison. The `create_index < min(comment_indices)` assertion
directly above is already same-list and stays as-is.

**C3 — fix `test_clone_runs_before_any_issue_work` (`:336-355`).** This is the
half the report misattributed. It asserts `api.calls[0][0] == "list_issues"`
plus `git.clone_called is True`, which still passes if the orchestrator listed
issues *before* cloning — so the clone-before-API ordering is claimed but never
pinned. Add a timeline assertion that the `clone` event precedes the first
`list_issues` event.

### Group D — finding 2: redundant clone-cleanup tests

`tests/test_git_service.py:993-1068`. The `_FakeRunner` raises before creating
the destination, so `assert not Path(cache_path).exists()` passes against a path
that never existed.

- **Delete** `test_clone_failure_removes_clone_path_before_raising` (`:993`) and
  `test_clone_keyboard_interrupt_removes_clone_path_and_reraises` (`:1026`).
  Both behaviors are covered with a real partial path at
  `tests/test_clone_cache.py:227` and `:243`.
- **Keep and fix** `test_clone_timeout_removes_clone_path_before_raising`
  (`:1049`): `Path(cache_path).mkdir()` before the call, then assert the path is
  gone. The timeout variant is retained deliberately — cleanup currently lives
  in one `finally` (`git.py:557-559`), but a refactor could move it into the two
  `except` clauses and silently stop cleaning up the timeout path. The retained
  test is the only thing that would catch that.

This group deletes two tests. That is a coverage change and needs explicit
sign-off, separate from the rest of the plan.

## Test contract (amendments, not new RED)

All eight items are amendments to existing tests; no new behavior is specified,
so no production change is expected. All eight are applied in RED.

1. `tests/test_package_boundaries.py` — both import-side-effect tests run in a
   fresh child process **and** walk-and-import every submodule; the false
   comment at `:107-108` is removed.
2. `tests/test_state_store.py` — the fsync test records `os.replace` on the same
   ordered log and asserts it precedes the directory fsync.
3. `tests/test_state_store.py` — the unwritable-directory test skips under root.
4. `tests/test_reporting.py` — the failed-count assertion targets `"1 failed"`.
5. `tests/test_cwd_freshness.py` — the ahead exclusion asserts **none** of the
   entries contains the secret branch.
6. `tests/test_orchestration.py` — the fake collaborators share one ordered
   timeline; the checkpoint-after-create assertion compares within it.
7. `tests/test_orchestration.py` — the clone-before-first-`list_issues` ordering
   is asserted on that timeline.
8. `tests/test_git_service.py` — the timeout cleanup test creates a partial path
   first; the two redundant duplicates are deleted.

## Verification

- `./scripts/run-tests.sh` — full suite green. Expected count 390 → 388, the
  delta being exactly the two Group D deletions.
- `mypy f2gh.py forgejo_to_github/` — clean. No production code is touched, so
  this is a guard against test edits tripping the checker, not a target.
- Lint and format are delegated to `@lint`, at the RED gate and again at GREEN
  if it becomes real. Never hand-run `ruff`.

## Out of scope

- **Editing `plans/10-test-audit-2026-09-26.md`.** It stays as the audit record.
  The corrections live in this plan's "Report provenance" section.
- Production changes **during RED**. None are expected; the guarded behavior is
  already correct per the table above. If an amendment fails, that is a finding
  about production: it makes GREEN real, and it is fixed only under GREEN's own
  approval.
- Coverage-gap analysis. The audit explicitly excluded it and this plan does not
  open it.
- New GitHub issues, new branches, and any remote operation.

## Staging

This is a test-remediation-only effort, so the sequence is RED and then, in the
ordinary case, nothing.

1. **RED.** Apply Groups A-D as the eight amendments above and run the
   verification suite. Report what passed and what did not. No production code is
   touched in this stage.
2. **Exit from RED** is the user approving and committing the updated tests. The
   agent does not declare this stage exited.
3. **GREEN is a no-op by default.** Every guarded behavior is already verified
   correct, so there is nothing to implement. It becomes real only if
   remediating the tests surfaces an actual implementation issue — and then only
   after its own approval.

## References

- `plans/10-test-audit-2026-09-26.md` — the audit report (unmodified)
- `forgejo_to_github/git.py:490-559` — `clone_into` and its cleanup `finally`
- `forgejo_to_github/state.py:511-535` — `_atomic_write_json` ordering
- `forgejo_to_github/reporting.py:262` — the failed-count label
- `tests/test_clone_cache.py:227-262` — real-partial-path cleanup coverage
- `AGENTS.md` §3 — staged gates; §4 — plan conventions
