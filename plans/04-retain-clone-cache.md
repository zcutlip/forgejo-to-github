# Retain successful git clone in state.json for retry

## Context

If migration fails after `git clone --mirror` succeeded (mirror in `/tmp` `f2gh-*`), we discard it via `shutil.rmtree` in `finally`, forcing re-clone on retry. For large repos or flaky network (exit 128 at clone), this wastes time/bandwidth.

Current flow clones to a tempdir (`/tmp` `f2gh-*`) and unconditionally cleans up in the `finally` block, even when the mirror was successfully created and could be reused. A failure in a later phase (e.g., issue import, rate limiting, interruption) therefore requires a full re-clone on the next run.

## Goal

Record clone location in `state.json` (e.g., `clone_path` field) when clone succeeds, so retry can reuse it instead of re-cloning. On resume, check path exists, is still a bare mirror (`git rev-parse --is-bare-repository`), and matches source (maybe check `remote.origin.url`).

- Write `clone_path` to `state.json` immediately after successful `git clone --mirror`.
- On resume/retry, validate cached mirror before reuse; fallback to fresh clone if validation fails.
- Preserve existing atomic `state.json` guarantees via `os.replace`.

## Considerations

- `tmpdir` is ephemeral (`/tmp`, may be GC'd), so consider persistent cache dir like `./.f2gh-mirror/` or `plans/` sibling, or keep `tmpdir` but handle missing case gracefully (fallback to re-clone).
- Need cleanup policy (remove on final success, or `f2gh --clean`).
- Atomic `state.json` via `os.replace` already handles determinism — extension must remain atomic and human-readable.
- Cache validation should be cheap and robust: `test -d`, `git rev-parse --is-bare-repository`, `git config --get remote.origin.url` comparison with source.
- Avoid unbounded disk growth; document/implement eviction or explicit clean-up.

## Relation to other notes

- Builds on clone-failure graceful handling.
- Complements local-clone optimization (avoid clone entirely when cwd is repo).

## Next steps

1. Design `state.json` schema extension (`clone_path`, maybe `clone_source`, `clone_created_at`).
2. Implement cache validation on startup/resume.
3. Add cleanup command/policy (`--clean` flag or automatic removal on final success).

## References

- Issue #1
- Session note #6
