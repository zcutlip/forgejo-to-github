# KeyboardInterrupt handling — table stakes for CLI

**GitHub issue:** [#4](https://github.com/zcutlip/forgejo-to-github/issues/4)

**Branch:** `dev/4-keyboard-interrupt-handling`

## Context

The CLI entry point is `f2gh:main` (`pyproject.toml` console script), and
the documented invocation is the installed `f2gh` command. Today the only
`KeyboardInterrupt` handling lives in the `__main__` guard
(`f2gh.py:210-214`):

```python
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Terminating.", file=sys.stderr)
```

That guard has three problems:

1. It only covers `./f2gh.py`, **not** the installed `f2gh` command, which
   calls `main()` directly.
2. It prints a bare `"Terminating."` with no resume guidance.
3. It exits `0` — a user-cancelled run reports success.

`main()` (`f2gh.py:196-207`) is the catch site with the context needed to
fix this: it has the parsed `args` (source/target) and can control the exit
code, replacing the current `sys.exit(reporter.exit_outcome(result))`.

### What is already safe

`state.json` is checkpointed atomically after each migrated issue and after
the git push (`StateStore._atomic_write_json` → `os.replace`,
`state.py:372`). A `Ctrl-C` cannot leave a partially-written state file, and
the last fully-migrated issue is already durable. **No phase-aware flush
work is required** — the issue's "decide what to preserve" question is
answered by the existing design.

### The real gap: clone tempdir leak

`GitMirror.clone` (`git.py:497-559`) creates the tempdir, runs
`git clone --mirror`, and returns the path **only on success**. On
`subprocess.TimeoutExpired`, `subprocess.CalledProcessError`, or a
`KeyboardInterrupt` raised while `git` is running, it raises without
cleaning up. The caller (`migration.prepare_repository`,
`migration.py:435-474`) can't clean up either, because it never receives the
path; its `finally` block only wraps the push, reached after a successful
clone. So an interrupt (or a plain clone failure) leaks an `f2gh-*` tempdir.

This is the one piece of genuine behavioral work. `GitMirror.cleanup`
(`git.py:633-653`) is idempotent and best-effort, so it is safe to call on
the failure path.

## Contract / desired behavior

- `Ctrl-C` at any point in `main()` after argument parsing:
  - prints a clear message (no traceback) to **stderr**;
  - flushes nothing (state is already durable);
  - exits `130` (SIGINT convention).
- The message is accurate about the state file:
  - normal run: `Interrupted by user — state saved to ./state.json, resume with f2gh --source SRC --target DST`
  - `--dry-run`: no state file is written, so the message must not claim
    state was saved. Dry-run prints: `Interrupted by user.`
- `GitMirror.clone` removes its tempdir when the clone fails *or* is
  interrupted, before re-raising. This is independent of the CLI catch.
- Other exceptions are unaffected: only `KeyboardInterrupt` is caught by the
  new handler; `SystemExit` (e.g. missing token) and real errors propagate
  unchanged.
- The `__main__` guard reduces to a plain `main()` call once `main` owns the
  handling (decision D).

## Decisions (locked)

- **A. Catch site — locked.** `KeyboardInterrupt` is caught in `main()`
  after `parse_args()`, wrapping `_build_orchestrator` + `run()` +
  render/exit. This covers the installed `f2gh` entry point and `./f2gh.py`,
  and gives the handler `args` for the resume hint. The `__main__` guard
  reduces to a bare `main()` call (see D).
- **B. Clone cleanup scope — locked.** `GitMirror.clone` cleans up its
  tempdir on **all** failure modes — interrupt, `TimeoutExpired`, and
  `CalledProcessError` — before re-raising. This closes the pre-existing
  clone-failure leak in the same code path rather than deferring it.
- **C. Message accuracy / state path — locked.** The resume hint prints the
  literal `./state.json` (hardcoded at `f2gh.py:105`); the message branches
  on `args.dry_run` so `--dry-run` never falsely claims state was saved.
- **D. `__main__` guard — locked.** Replaced with a bare `main()` call; no
  defensive catch, single source of truth.
- **E. Exit-code home — locked.** `EXIT_INTERRUPTED = 130` is defined in the
  CLI layer, not `reporting.py`, since #7 relocates `exit_outcome`.
- **F. Output channel — locked (user choice).** The interrupt banner is
  written directly to stderr by the CLI, not routed through a new `Reporter`
  method. Rationale (user): fatal-path reporting must stay a simple pipeline
  so we do not compound errors while already reporting shutdown. Normal
  reporting remains Reporter-owned per memory #284, and `Reporter` stays
  within its 9-public-method cap (memory #271).

## Out of scope

- Phase-aware state flushing (unnecessary — see above).
- `signal`-handler-based masking.
- Changing `render_final` / `exit_outcome` ownership (that is #7).
- Interrupt handling inside the orchestrator class; `run()` continues to
  never call `sys.exit` and never catch `KeyboardInterrupt`.

## Test contract (RED stage)

New/amended tests, written before implementation:

1. `tests/test_cli.py` — interrupt in `run()`:
   fake orchestrator raises `KeyboardInterrupt`; patch `sys.exit`; assert
   `SystemExit.code == 130`, stderr contains the resume hint with the exact
   `--source`/`--target`, and no traceback on stdout/stderr.
2. `tests/test_cli.py` — dry-run interrupt: assert the dry-run message does
   not claim `state saved`.
3. `tests/test_cli.py` — non-`KeyboardInterrupt` from `run()` propagates
   (guards against an over-broad `except`).
4. `tests/test_git_service.py` — `GitMirror.clone` cleans up on failure:
   command runner raises `CalledProcessError` → cleanup called and tempdir
   gone, error re-raised.
5. `tests/test_git_service.py` — `GitMirror.clone` cleans up on interrupt:
   command runner raises `KeyboardInterrupt` → cleanup called, tempdir gone,
   `KeyboardInterrupt` re-raised.

## References

- `plans/archive/01-clone-failure-followup.md` — adjacent clean-exit contract.
- `plans/04-retain-clone-cache.md`, `plans/05-local-clone-invocation.md` —
  both change clone lifecycle; keep the cleanup contract compatible.
- GitHub issue #7 — Reporter seam cleanup (owns exit-code relocation).
- Memory #271 (Reporter cap 9), #284 (Reporter output routing).
