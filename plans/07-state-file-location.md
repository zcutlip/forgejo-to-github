# state.json location depends on cwd — per-migration default outside the working directory

**GitHub issue:** [#14](https://github.com/zcutlip/forgejo-to-github/issues/14)

**Branch:** `dev/14-state-file-location`

## Context

`state.json` is read and written relative to the current working directory:
`f2gh.py` hardcodes `state_path = Path("state.json")`. The consequences:

- The cwd must be writable, and it must be the *same* directory on every
  run. Re-running the same migration from elsewhere silently starts over
  from empty state.
- It implies you should run from the project directory being migrated,
  but the tool never reads that local checkout — the coupling buys
  nothing.
- Two different migrations run from one directory share a single file.
  The identity check (`load` returns fresh defaults when `source`/`target`
  mismatch) means the second run loads empty, then its next `save`
  destroys the first migration's checkpoint.
- It drops `state.json` into whatever directory the user happened to be in.

This is a new-issue design change; no backward-compatibility constraint is
claimed (single user, no external consumers).

Implementation surfaced a second defect on this same path: the new nested
default means the state file's parent directory usually does not exist yet,
and nothing in the write path created it. Combined with the orchestrator's
best-effort handling of persistence errors, every checkpoint write failed
silently — the tool reported success while recording nothing. This plan
closes the first gap by establishing the state path before any mutating
operation. What a *later* loss of that path should do — and the best-effort
handling itself — is deliberately left to a separate issue.

## What is already safe

- `StateStore(state_path, source, target)` takes an **explicit** path and
  has no default of its own. The CLI resolves the path and passes it in.
  The constructor and the `save`/`load` signatures are unchanged, so the
  existing `tests/test_state_store.py` contract tests stay valid. This
  plan adds one method to the class (`prepare`, Decisions H).
- `source`/`target` are already validated and split safely into
  `OWNER`/`REPO` (`f2gh.py`, the `_build_orchestrator` shape check). The
  new path helper can assume well-formed `owner/repo` inputs.
- The final-summary `State:` line already prints the resolved path:
  `reporting.py` renders `discovery.state_path`, which `migration.py` sets
  from `state.state_path`. No change is required there.
- Atomic writes (`os.replace`) are unchanged — only the file's location
  moves.

## Contract / desired behavior

- **Default path** (no override):
  `platformdirs.user_state_dir("f2gh") / <src-owner> / <src-repo> /
  <tgt-owner> / <tgt-repo> / state.json`
  - Linux: `~/.local/state/f2gh/...`
  - macOS: `~/Library/Application Support/f2gh/...`
  - Windows: `%LOCALAPPDATA%\f2gh\...`
- **`--state-file PATH` overrides** the default and is used verbatim. It is
  an option, never a requirement — the tool always has a sane default.
- **`StateStore` constructor is unchanged.** The CLI computes the resolved
  path and passes it to `StateStore(state_path, source, target)`.
- **The interrupt banner prints the resolved path** instead of the
  hardcoded `./state.json` (`f2gh.py`, the `KeyboardInterrupt` handler).
- **A legacy `./state.json` in cwd is ignored entirely** — not read, not
  warned about, not migrated.
- **Resolution happens after source/target validation.** The path currently
  is set before `source`/`target` are parsed; the new order must be
  validate → resolve → construct.
- **The state path is created, verified, and exclusively claimed before any
  mutating operation.** `StateStore.prepare()` creates the state file's
  parent directories if absent, writes a probe file and reads it back to
  confirm the bytes actually round-trip (not merely that a file could be
  created), and acquires the run lock. Failures are typed: `StateWriteError`
  when the path cannot be created or verified, `StateLockedError` when
  another run already holds the lock — so the CLI can distinguish "this path
  is unusable" from "someone else is using it". `MigrationOrchestrator.run()`
  calls it (concrete store only) after the checkpoint load and **before**
  `_prepare_target` — the boundary between read-only work (dry-run
  discovery, checkpoint load) and destination writes (repository create,
  git push, issue create). Dry-run returns before that point and still
  touches nothing.
- **The state path is exclusively ours for the duration of the run.** A
  lock file named `<state>.lock` sits beside the state file, so two runs of
  the *same* migration contend while runs of different migrations never do.
  Contention is a refusal to start, not a warning — two runs sharing one
  checkpoint is exactly the duplicate-issue outcome this tool exists to
  avoid. The lock is taken by `prepare()` and dropped by
  `StateStore.release()`, which `run()` calls in a `finally`; the OS
  additionally drops it when the process ends, so a crash needs no
  stale-lock recovery and there is no cleanup path to get wrong. Release
  drops the lock and **leaves the lock file on disk** — removing it would
  reintroduce the classic unlink race, where a waiter that already opened
  the file and a newcomer that re-creates it hold locks on different inodes
  and both believe they have it. The file's presence carries no meaning
  once its lock is gone.
- **A refused preflight stops the run with a clear reason.** When
  `prepare()` cannot establish the state path, the CLI reports the resolved
  path and exits `EXIT_STATE_ERROR` (`3`), never starting the migration.
  Contention and an unusable path are reported distinctly, because "another
  run is already migrating this pair" and "this path cannot be written" call
  for different responses from the operator.
- **The checkpoint rename is durable, not merely atomic.**
  `_atomic_write_json` already fsyncs its temp file before `os.replace`; it
  must also fsync the parent directory afterwards so the rename itself
  survives a power loss. The directory fsync is POSIX-only — Windows
  exposes no equivalent through `os` — and is skipped there.
- **README reflects the new location.** The resumability bullet currently
  names a bare `state.json` and tells the user to delete it after deleting
  the GitHub repository. It must state the resolved default location, note
  `--state-file` as an override, and keep the "delete state to reset"
  guidance pointing at a real path.
- **New dependencies:** `platformdirs` (path resolution) and `filelock`
  (the run lock). Both are single-purpose with no transitive dependencies.
  `filelock` dispatches to `fcntl.flock` on POSIX and `msvcrt.locking` on
  Windows — the platform branch we cannot test ourselves, and therefore
  better delegated to a library whose users exercise it.
- **New exception:** `StateLockedError` in `state.py`, a sibling of
  `StateWriteError` rather than a subclass — lock contention is not a write
  failure, and the CLI needs to tell the two apart.

## Decisions

- **A. Default location.** Platform *user-state* directory
  (`platformdirs.user_state_dir`), not the cache directory. State is
  durable, resume-critical data; a cache dir may be purged by the OS.
- **B. Namespacing.** Per-migration, nested directories derived from the
  source→target pair. Nested layout avoids the separator-ambiguity problem
  of a flat slug (owner/repo names may contain `-`, `_`, `.`).
- **C. Override.** `--state-file` is an escape hatch, not a requirement.
- **D. Helper split.** The path logic is split so the platform call is
  isolated from the layout rule:
  - a **pure** `state_path_for(base, source, target)` that only performs
    layout, and
  - a single `default_state_base()` that calls
    `platformdirs.user_state_dir("f2gh")`.
  This keeps the layout rule testable with concrete `tmp_path` inputs and
  no test-only parameters in the production signature.
- **E. Helper home.** A new `forgejo_to_github/paths.py`. The clone-cache
  helper (plan 04 / #5) joins it there, so the per-migration path scheme
  lives in one place.
- **F. Banner.** The interrupt handler prints the actual resolved path.
- **G. Legacy file.** Ignored outright.
- **H. Preflight.** A `StateStore.prepare()` method (create directories,
  write a probe and read it back to confirm the bytes round-trip) invoked by
  `MigrationOrchestrator.run()` before `_prepare_target`. An
  existing-but-unwritable directory passes `mkdir` and still fails at the
  first real checkpoint, which lands *after* the destination repository
  has been created and the mirror pushed; proving writability up front
  turns that into a refusal to start. The mechanism lives on `StateStore`
  (which owns the path), the ordering lives in the orchestrator (which
  owns the phases). It is deliberately **not** implemented as a preflight
  `save()` — several tests pin `save()` call counts.
  The preflight is also the *baseline* the run relies on: once it has
  confirmed the path exists and is writable, any later loss of that path is
  a change from a known-good state rather than an ambiguous absence. This
  plan stops at establishing that baseline; what a later loss should do is
  the separate issue's contract.
- **J. Run lock.** A per-migration lock file at `<state>.lock`, acquired by
  `prepare()` via `filelock.FileLock(..., timeout=0)`. `timeout=0` gives
  exactly one acquisition attempt, which is the "refuse to start" semantics
  required. Contention raises a **distinct** `StateLockedError` rather than
  `StateWriteError`, because "someone else is using this path" is a
  different condition from "this path is unusable" and the CLI should be
  able to say which. Release is **explicit**: `StateStore.release()` drops
  the lock and is idempotent, and `run()` calls it in a `finally` so the
  lock cannot outlive the migration, and leaves the lock file on disk
  rather than unlinking it (see the lock contract bullet for why). Process
  exit remains the crash backstop — the OS drops the lock however the
  process ends, which is why this style of locking needs no stale-lock
  recovery. Locking beside the state file rather than globally keeps
  unrelated migrations independent.
  `fallback_to_soft` is disabled so a filesystem that cannot provide real
  locks fails loudly instead of silently degrading to existence-based
  locking. The refusal reaches the operator as `EXIT_STATE_ERROR` (`3`),
  sitting alongside the reserved `0`/`1`/`2` migration outcomes and `130`
  for an interrupt. The fail-fast work reuses this code rather than
  defining its own — both conditions mean the same thing operationally,
  that the state path refused.
- **K. Durable rename.** After `os.replace`, fsync the parent directory so
  the rename is durable and not only atomic. Guarded for platforms that
  cannot fsync a directory; the POSIX path is the one the suite covers.

## Out of scope

- Reading, warning about, or migrating a legacy `./state.json`.
- Any state-file *schema* change: `clone_path`/`clone_source` for the
  clone cache is #5; the `version` key is #11.
- The clone-cache feature itself (#5) — this plan only establishes the
  shared path helper it will consume.
- `CodebergClient` pagination (#9) and arbitrary Forgejo instances (#13).
- **Windows test-suite readiness.** The tool is written portably and should
  run on Windows, but the suite is validated on POSIX only and cannot be
  validated there. The `chmod`-based unwritable-directory test is the known
  POSIX-dependent case; it is left as-is rather than guarded, since running
  the suite on Windows is not planned.
- **Making a persistence failure abort the run.** This plan establishes the
  state path up front but deliberately does not decide what a *later* loss
  of it should do. That belongs to the issue that makes persistence
  failures abort — including the rule that a path confirmed good and then
  lost is fatal — and changes the orchestrator's contract for every
  migration.

## Test contract (RED stage)

Written before implementation:

1. `tests/test_paths.py` — pure layout: given a `tmp_path` base and a
   `source`/`target` pair, `state_path_for` returns
   `<base>/<src-owner>/<src-repo>/<tgt-owner>/<tgt-repo>/state.json`.
2. `tests/test_paths.py` — default base: `default_state_base()` routes
   through `platformdirs.user_state_dir("f2gh")` (one focused check).
3. `tests/test_paths.py` — identity isolation: same pair yields the same
   path regardless of cwd; two different pairs yield different paths.
4. `tests/test_cli.py` — an explicit `--state-file` is honored verbatim;
   the CLI does not namespace or rewrite it.
5. `tests/test_cli.py` — the interrupt banner prints the resolved path,
   not `./state.json` (amends the existing banner assertion).
6. `tests/test_state_store.py` — `prepare()` creates missing parent
   directories and succeeds; it raises `StateWriteError` when the
   directory cannot be created or written.
7. `tests/test_orchestration.py` — `run()` calls `prepare()` before the
   first mutating phase: when `prepare()` raises, no repository-create
   call is made. Also that a completed run releases the lock — a fresh
   store on the same path can `prepare()` immediately afterwards.
8. `tests/test_state_store.py` — `save` into a path whose parent does not
   exist raises `StateWriteError` rather than creating it. A **disclosed
   guard**: it passes today and cannot fail until someone adds directory
   creation to the writer. It exists so a silent `mkdir` cannot be
   reintroduced unnoticed.
9. `tests/test_state_store.py` — the probe verifies content, not just
   creatability: `prepare()` raises `StateWriteError` when a written probe
   does not read back as written.
10. `tests/test_state_store.py` — the run lock: a second `prepare()` on the
    same path raises `StateLockedError` while the first holds it; releasing
    the first lets the second through; `release()` is idempotent; two
    different state paths never contend.
11. `tests/test_state_store.py` — `save` fsyncs the parent directory after
    `os.replace` (mechanism assertion, in the style of the existing
    `os.replace` spy).
12. `tests/test_cli.py` — a refused preflight exits `EXIT_STATE_ERROR` (`3`)
    without starting the migration, and the message names the resolved
    state path. Lock contention and an unusable path produce
    distinguishable output.

## References

- GitHub issue #14
- `plans/04-retain-clone-cache.md` / #5 — consumes the same path helper for
  the clone cache (`platformdirs.user_cache_dir`)
- GitHub issue #11 — state `version` key (adjacent schema work, not here)
- `.cache`/`.state` convention note: no legacy migration is required, so no
  compatibility test is added
- `filelock` — run lock; dispatches to `fcntl.flock` (POSIX) /
  `msvcrt.locking` (Windows). Chosen over hand-rolling precisely because the
  Windows branch is untestable here.
