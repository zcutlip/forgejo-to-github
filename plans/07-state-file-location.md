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
silently — the tool reported success while recording nothing. The
preflight and writer-backstop requirements below close that gap; the
best-effort handling itself is tracked separately.

## What is already safe

- `StateStore(state_path, source, target)` takes an **explicit** path and
  has no default of its own. The CLI resolves the path and passes it in.
  The constructor and the `save`/`load` signatures are unchanged, so the
  existing `tests/test_state_store.py` contract tests stay valid. This
  plan adds one method to the class (`prepare`, Decisions H) and one guard
  inside the existing atomic-write helper (Decisions I).
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
- **The state directory is created and proven writable before any mutating
  operation.** `StateStore.prepare()` creates the state file's parent
  directories if absent, then verifies writability with a
  create-and-remove probe file in that directory, raising
  `StateWriteError` on failure. `MigrationOrchestrator.run()` calls it
  (concrete store only) after the checkpoint load and **before**
  `_prepare_target` — the boundary between read-only work (dry-run
  discovery, checkpoint load) and destination writes (repository create,
  git push, issue create). Dry-run returns before that point and still
  touches nothing.
- **The atomic-write helper creates its parent directory as a backstop.**
  `_atomic_write_json` ensures `path.parent` exists before opening its
  `.tmp` sibling, so a state directory removed mid-run is recreated rather
  than turning a `save` into a failure.
- **README reflects the new location.** The resumability bullet currently
  names a bare `state.json` and tells the user to delete it after deleting
  the GitHub repository. It must state the resolved default location, note
  `--state-file` as an override, and keep the "delete state to reset"
  guidance pointing at a real path.
- **New dependency:** `platformdirs` (single-purpose, no transitive
  dependencies), added to the current minimal dependency set (`requests`).

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
  then prove writability with a temp-file probe) invoked by
  `MigrationOrchestrator.run()` before `_prepare_target`. An
  existing-but-unwritable directory passes `mkdir` and still fails at the
  first real checkpoint, which lands *after* the destination repository
  has been created and the mirror pushed; proving writability up front
  turns that into a refusal to start. The mechanism lives on `StateStore`
  (which owns the path), the ordering lives in the orchestrator (which
  owns the phases). It is deliberately **not** implemented as a preflight
  `save()` — several tests pin `save()` call counts.
- **I. Writer backstop.** `_atomic_write_json` creates `path.parent` before
  opening its temp file. The preflight is about *timing* — fail before the
  destination is touched; this is about *correctness* — a `save` must
  succeed even if the directory disappeared after the preflight ran.

## Out of scope

- Reading, warning about, or migrating a legacy `./state.json`.
- Any state-file *schema* change: `clone_path`/`clone_source` for the
  clone cache is #5; the `version` key is #11.
- The clone-cache feature itself (#5) — this plan only establishes the
  shared path helper it will consume.
- `CodebergClient` pagination (#9) and arbitrary Forgejo instances (#13).
- **Making a persistence failure abort the run.** Whether a `save` failure
  should stop the migration rather than being swallowed changes the
  orchestrator's contract for *every* migration and is tracked as its own
  issue. This plan only ensures the failure cannot arise from a missing
  directory, and that the destination is not touched when the preflight
  cannot write.

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
6. `tests/test_state_store.py` — `save` into a path whose parent does not
   exist creates the parent and round-trips through `load`.
7. `tests/test_state_store.py` — `prepare()` creates missing parent
   directories and succeeds; it raises `StateWriteError` when the
   directory cannot be created or written.
8. `tests/test_orchestration.py` — `run()` calls `prepare()` before the
   first mutating phase: when `prepare()` raises, no repository-create
   call is made.
9. `tests/test_orchestration.py` — a state directory removed partway
   through a run (after the preflight, before the next checkpoint) is
   recreated by the writer backstop and the run completes. This is the
   end-to-end proof that Decisions H and I hold through a real run, not
   merely through a direct `save()` call.

## References

- GitHub issue #14
- `plans/04-retain-clone-cache.md` / #5 — consumes the same path helper for
  the clone cache (`platformdirs.user_cache_dir`)
- GitHub issue #11 — state `version` key (adjacent schema work, not here)
- `.cache`/`.state` convention note: no legacy migration is required, so no
  compatibility test is added
