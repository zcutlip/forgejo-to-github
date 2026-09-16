# Retain successful git clone in state.json for retry

**GitHub issue:** [#5](https://github.com/zcutlip/forgejo-to-github/issues/5)

**Branch:** `dev/5-retain-clone-cache`

**Target release:** 1.3.0 (dev version `1.3.0.dev0` on branch; drop `.dev0` at release)

## Context

If migration fails after `git clone --mirror` succeeded, the orchestrator
(`migration.py::_run_git_phase`) unconditionally calls
`cleanup(local_path)` in a `finally` block — discarding a good mirror and
forcing a full re-clone on retry. For large repos or flaky network (exit
128 at clone), this wastes time/bandwidth.

Resume today is binary: `git_pushed` truthy → skip the whole Git phase;
otherwise re-clone from scratch. There is no intermediate
"cloned but not yet pushed" checkpoint.

This plan builds on #14 (per-migration state path under
`platformdirs.user_state_dir("f2gh")`) and #15 (state-write failures are
fail-fast, exit 3). The same per-migration layout helper feeds the clone
cache, rooted at `platformdirs.user_cache_dir("f2gh")`.

## Contract / desired behavior

1. **Cache location.** New `cache_path_for(base, source, target)` in
   `paths.py` → `<base>/<src-owner>/<src-repo>/<tgt-owner>/<tgt-repo>/mirror.git`,
   where `base` is `user_cache_dir("f2gh")`. The CLI resolves it
   (validate → resolve → construct, same order as #14). `GitMirror` and
   `StateStore` take explicit values; neither gains a path default. Never
   cwd-relative.
2. **GitMirror owns git knowledge.** Two new methods (stays under the
   9-public-method cap):
   - `clone_into(path)`: `git clone --mirror` into the given directory;
     a partial directory is removed on failure (current behavior
     preserved). Clone failure stays terminal. `clone_into` is the sole
     clone entry: the existing `clone()` method and the `tempdir_factory`
     seam are removed (the ephemeral-`/tmp` path is the design mistake
     this issue retires). The existing `clone()`/`tempdir_factory` call
     sites in the test suite are updated under an explicit RED reopen.
   - `cached_mirror_is_valid(path)`: directory exists, `git rev-parse
     --is-bare-repository`, and `git config --get remote.origin.url`
     matches the live source URL — all through the injected
     `command_runner` seam. Push failure stays non-fatal.
3. **Orchestrator resume.** On resume with a valid cached mirror, skip the
   clone and push from the cache (push-as-is: no fetch/refresh — the
   mirror is a frozen snapshot, deterministic). Invalid or missing cache
   → fresh clone (fallback, never a hard error): an invalid cached mirror
   is removed via the injected `cleanup` seam — scoped strictly to the
   resolved cache path — before re-clone. If removal itself fails, that
   is a hard error.
4. **Checkpoint immediately after clone.** The `clone_path` is persisted
   through the state seam right after a successful clone, before any push.
   A write failure aborts the run (exit 3, per #15) — never swallowed.
   Push success → `_mark_git_pushed()` as today, then delete the cache.
   Push failure → keep the cache so the next run retries the push without
   re-cloning.
5. **Schema.** `ACCEPTED_KEYS` += `"clone_path"`; `StateStore.save()`
   gains optional `clone_path: str | None = None`; `load()` returns it
   (absent → `None`, so legacy files stay valid). No version bump: no
   backward-compatibility requirement (single user). Old `save()` calls
   keep working, so locked tests stay green without a RED reopen.
6. **`f2gh --clean`.** Removes this migration's cached mirror — and only
   that. Reports what it did. Under `--dry-run` it prints what it *would*
   remove and deletes nothing. Refuses while the state lock is held by
   another run.
7. **Untouched:** dry-run performs no clone (no cache interaction);
   clone terminal / push non-fatal semantics; atomic `os.replace` writes.

## Decisions (user-approved)

- **Cleanup policy:** delete the cache on full migration success; keep it
  for retries; `--clean` flag in this issue for manual eviction.
- **Schema placement:** `clone_path` key in `state.json` (not a sidecar file).
- **Staleness:** push-as-is on resume, no refresh fetch.

## RED (tests lock this contract; separate gate)

- Orchestration test: post-clone state carries the **producer-set**
  `clone_path` (producer-driven value, not just store round-trip).
- Resume test: valid cache → clone skipped, push retried (concrete
  collaborators with mocked I/O, not broad fakes).
- Invalid-cache test: corrupt/missing/wrong-origin cache → fresh clone.
- `--clean` tests: real delete, dry-run deletes nothing, lock-held refusal.
- KeyboardInterrupt-safety: wrap in `try/except KeyboardInterrupt →
  pytest.fail` (never let it escape under xdist).
- Each new test must fail for the contract reason before GREEN.

## References

- Issue #1 (original clone-failure handling)
- #14 / `plans/07-state-file-location.md` (per-migration state path)
- #15 / `plans/08-state-write-fail-fast.md` (fail-fast state writes)
- #6 / `plans/05-local-clone-invocation.md` (follow-up: skip clone from local checkout)
