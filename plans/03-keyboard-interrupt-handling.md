# KeyboardInterrupt handling — table stakes for CLI

**GitHub issue:** [#4](https://github.com/zcutlip/forgejo-to-github/issues/4)

## Context

Current `f2gh.py` has at line 528-531:

```python
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Terminating.", file=sys.stderr)
```

This catches `Ctrl-C` only at the top-level entry point. Migrate phases — repo creation, git mirror clone/push at `237`/`284`/`290`, per-issue loop — may be mid-operation when `KeyboardInterrupt` fires, leaving `tmpdir`, `state.json`, or a partial git push inconsistent if `Ctrl-C` happens during a blocking call.

## Desired behavior

- Catch `KeyboardInterrupt` at `migrate` top-level or per-phase (not just `__main__`).
- Ensure `tmpdir` cleanup via `finally` already present in `mirror_git_repo`.
- Ensure `state.json` remains deterministic (atomic `os.replace`) — no partial writes.
- Print clear message instead of traceback:

  ```
  Interrupted by user — state saved to ./state.json, resume with f2gh --source SRC --target DST
  ```

- Exit code `130` (standard `SIGINT` convention).
- No token leak in output (redact via `_redact_token` pattern).

## Adjacent note

Adjacent to clone failure note (`plans/01-clone-failure-followup.md`) — both are about graceful exit completeness. Clone note covers `CalledProcessError` on `git clone --mirror`; this note covers user-initiated `SIGINT`. Together they define the "clean exit" contract.

## Next steps (for later discussion)

- Decide where to catch: `migrate` wrapper vs `main` — wrapper preserves phase-aware state save, `main` is simpler.
- Decide what to preserve: flushed `state.json` with last fully-migrated issue, deterministic stats.
- Test with `kill -INT` during `clone` (longest blocking call) and during per-issue loop.
- Consider `signal` handler vs `try/except KeyboardInterrupt` — prefer `try/except` to avoid signal-masking complexity.

## Reference

GitHub issue #1 — https://github.com/zcutlip/forgejo-to-github/issues/1
