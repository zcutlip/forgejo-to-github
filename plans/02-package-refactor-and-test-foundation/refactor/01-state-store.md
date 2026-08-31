# Stage 01 — StateStore and `MigrationState`

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** none.
**Blocks:** stage 02 (clients), stage 03 (git), stage 04 (orchestrator),
stage 06 (CLI wiring).

## 1. Objective

Replace the module-level `load_state` / `save_state` free functions and
the `STATE_FILE = "state.json"` module global in `f2gh.py` with a
proper, instance-owned `StateStore` class plus a typed `MigrationState`
dataclass. The class must support:

- Loading from an arbitrary path with explicit source/target identity.
- Loading from a missing file (fresh defaults, no exception).
- Loading from a mismatched-identity file (treat as fresh).
- Saving atomically via a temp file plus `os.replace`.
- Surfacing malformed JSON and unsupported-state-version conditions as
  structured exceptions, not `json.JSONDecodeError` or `OSError`.
- Loading and saving the legacy on-disk JSON shape so existing files
  from `f2gh.save_state` remain usable.

The store is the only module permitted to touch the on-disk state file.
The orchestrator and CLI do not write the file directly.

## 2. Files / modules

- **New module:** `forgejo_to_github/state.py` containing:
  - `MigrationState` (frozen `@dataclass`).
  - `IssueCheckpoint` (frozen `@dataclass`).
  - `StateStore` (regular `@dataclass` or class; signature is locked).
  - `StateLoadError` (exception).
  - `StateWriteError` (exception).
  - Module-private atomic write helper.

The `forgejo_to_github/__init__.py` is unchanged during this stage.
`forgejo_to_github/state.py` becomes a new submodule of the existing
package and is referenced by name in `test_package_boundaries.py`'s
`EXPECTED_PUBLIC_CLASSES`.

## 3. Public API and responsibilities

### 3.1 `MigrationState` and `IssueCheckpoint`

Frozen dataclasses. Plain Python dataclass, no pydantic, no attrs.

```python
@dataclass(frozen=True)
class IssueCheckpoint:
    source_number: int
    github_number: int
    state: str
    closed: bool

@dataclass(frozen=True)
class MigrationState:
    source: str
    target: str
    repo_created: bool
    git_pushed: bool
    migrated: dict[int, IssueCheckpoint]
```

Field notes:

- `migrated` keys are Codeberg issue numbers (int). Values are
  `IssueCheckpoint` records.
- The on-disk JSON serialization reduces `migrated` to a flat
  `dict[str, int]` mapping source issue number (as a string) to GitHub
  issue number. The `state` and `closed` fields are **not** persisted
  in this revision. On reload, `closed` is reconstructed as `False`
  for all entries. The on-disk format therefore remains
  `{"source": ..., "target": ..., "migrated": {"<src>": <gh>}}`,
  which is the legacy format produced by `f2gh.save_state`.
- The decision to omit per-issue comment progress from `IssueCheckpoint`
  is a deliberate simplification. The GitHub API does not provide
  per-issue atomicity (there is no way to roll back a partially posted
  issue), and per-comment progress persistence is not a goal of plan
  02. **Resume of a partially completed issue re-creates all comments
  and re-issues the close.** This is approved at the spec-review step;
  it is the locked behavior for this plan.

### 3.2 `StateStore`

Class. Constructor signature is locked:

```python
StateStore(state_path: Path, source: str, target: str)
```

`state_path` is a `pathlib.Path`. There is no default value; calling
`StateStore()` is a `TypeError`. The class does not read or write any
module-level `STATE_FILE`; importing the module must not surface a
canonical path constant.

Methods:

- `load() -> MigrationState`
  - If `state_path` does not exist: return a fresh `MigrationState`
    with `source=source, target=target, repo_created=False,
    git_pushed=False, migrated={}`.
  - If `state_path` exists and JSON is malformed: raise `StateLoadError`
    with `reason` set to a redaction-safe message and `path` set to
    `state_path`.
  - If `state_path` exists and `source` or `target` field does not
    match the constructor's identity: return a fresh state (legacy
    compatibility rule, preserved from `f2gh.load_state`).
  - If `state_path` exists and contains a `"version"` key not in the
    set `{1}` (current version is implicit): raise `StateLoadError`
    with message containing "unsupported state version". Absent version
    is acceptable for files written by the legacy `f2gh.save_state`,
    which never wrote a version field.
  - If `state_path` exists and contains a `"version"` key not parseable
    as an integer: raise `StateLoadError` with `reason` referencing
    "version".
  - If `state_path` exists and contains unexpected top-level keys
    beyond the documented set (`{"source", "target", "migrated",
    "repo_created", "git_pushed", "version"}`): raise `StateLoadError`
    with `reason` referencing the offending key. The check is performed
    by a documented `ACCEPTED_KEYS` constant.
  - If `state_path` exists and `migrated` values are not parseable as
    integers (e.g., a string): raise `StateLoadError` with `reason`
    referencing "migrated".
  - On success: return a `MigrationState` whose `migrated` keys are
    coerced back to `int` and whose values are `IssueCheckpoint`
    instances with `closed=False` (per §3.1).

- `save(state: MigrationState) -> None`
  - Serialize the supplied `MigrationState` to JSON, with `migrated`
    reduced to `dict[str, int]` (source number → GitHub number) per
    §3.1.
  - The current `source`, `target`, `repo_created`, and `git_pushed`
    are taken from the supplied `state`. The `StateStore` constructor's
    `source` and `target` are not re-checked at save time; the caller
    is responsible for passing a state whose identity matches.
  - Write to `<state_path>.tmp` in the same directory.
  - `fsync` the temp file.
  - `os.replace` the temp file onto `state_path`.
  - Permissions errors raise `StateWriteError`, not `OSError` /
    `PermissionError`.
  - The saved JSON is human-readable: `indent=2`, `sort_keys=True`,
    trailing newline.

### 3.3 Exceptions

- `class StateLoadError(Exception)`: carries `path: Path` and
  `reason: str`. Constructed with `(path, reason)` or `(path, reason,
  original)`. The string form is the redaction-safe `reason`.
- `class StateWriteError(Exception)`: carries `path: Path` and
  `reason: str`. Same construction signature.

Both exceptions must never carry the contents of the state file in
their messages, even if the file contained a token by accident.

### 3.4 Atomic write helper

Module-private: `_atomic_write_json(path: Path, payload: dict) -> None`.
Performs the temp-file + fsync + `os.replace` sequence. Raises
`StateWriteError` on `OSError`. The orchestrator and CLI do not call
this helper directly; it is an implementation detail of `StateStore.save`.

### 3.5 Accepted-keys constant

```python
ACCEPTED_KEYS: frozenset[str] = frozenset(
    {"source", "target", "migrated", "repo_created", "git_pushed", "version"}
)
```

`load()` raises `StateLoadError` for any top-level key outside this set.

## 4. Invariants

- **Single ownership of the state path.** No module-level constant for
  the state path exists in `forgejo_to_github.state` or in
  `forgejo_to_github`. The CLI is the only place that derives the path
  (from CLI args or the working directory).
- **Identity mismatch is silent.** When `source` or `target` differs
  from what the file records, the store returns a fresh state. It does
  not raise. This preserves legacy `f2gh.load_state` semantics.
- **Atomicity.** A crash mid-`save()` never leaves the destination
  file in a partially written state. The destination either retains
  its prior contents (if `os.replace` did not run) or contains the new
  full payload (if it did).
- **Round-trip equality.** `state == StateStore(...).save(state);
  reload` for the fields that round-trip (`source`, `target`,
  `repo_created`, `git_pushed`, and the `source_number`/`github_number`
  pair for each `IssueCheckpoint`). `closed` is not round-tripped.
- **No network, no subprocess.** `forgejo_to_github.state` imports only
  from the standard library.

## 5. Collaborator / dependency rules

- `StateStore` accepts no collaborators. It depends only on the
  filesystem and the standard library.
- `MigrationState` and `IssueCheckpoint` are pure value objects; no
  methods perform I/O.
- The atomic write helper must not be exposed publicly. Tests that need
  to assert the `os.replace` call use `unittest.mock.patch` on
  `os.replace` from within `forgejo_to_github.state`'s namespace.

## 6. Migration / compatibility constraints

- **Legacy `f2gh.load_state` and `f2gh.save_state` stay in place during
  stage 01.** The CLI still uses them, and `tests/test_state.py`
  (`test_save_and_load_state_round_trip`) and
  `tests/test_characterization.py` (`test_load_state_returns_fresh_defaults_when_*`,
  `test_save_state_uses_os_replace_for_atomic_write`) still pass against
  the legacy functions. They are removed in stage 06 (CLI wiring)
  once the new orchestrator is wired to `StateStore`. The test rewrite
  is governed by `06-cli-wiring.md` §6.
- **JSON on disk must remain backward-compatible.** Files produced by
  the legacy `save_state` (no `version` field, integer keys serialized
  as strings) must load successfully in the new `StateStore.load`. This
  is the contract asserted by
  `test_load_ignores_checkpoint_with_mismatched_source` /
  `_target` (identity mismatch) and indirectly by `test_round_trip_restores_int_keys`.
- **Version field is optional.** Files without a `"version"` key are
  accepted; absence is not an error. Only a present-but-unsupported or
  non-integer version is an error.
- **Unknown keys are rejected.** The `ACCEPTED_KEYS` check is part of
  the public contract. A future version of the format may add new keys
  by updating `ACCEPTED_KEYS`; this is an explicit change to the public
  contract.

## 7. Test references

The following tests assert the `StateStore` contract and must pass
green at the end of this stage:

- `tests/test_state_store.py::test_load_returns_default_state_when_file_absent`
- `tests/test_state_store.py::test_load_ignores_checkpoint_with_mismatched_source`
- `tests/test_state_store.py::test_load_ignores_checkpoint_with_mismatched_target`
- `tests/test_state_store.py::test_save_persists_all_fields_and_uses_atomic_replace`
- `tests/test_state_store.py::test_save_calls_os_replace_for_atomic_write`
- `tests/test_state_store.py::test_round_trip_restores_int_keys`
- `tests/test_state_store.py::test_state_store_does_not_expose_module_level_state_file`
- `tests/test_state_store.py::test_state_store_uses_instance_path_not_module_global`
- `tests/test_state_store.py::test_state_store_constructor_takes_path_source_target`
- `tests/test_state_store.py::test_save_signature_locked`
- `tests/test_package_boundaries.py::test_intended_public_class_is_importable`
  (parameterized for `forgejo_to_github.state.StateStore`)
- `tests/test_package_boundaries.py::test_public_class_has_docstring`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_least_two_public_methods`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_most_seven_public_methods`
  (same)
- `tests/test_package_boundaries.py::test_state_store_constructor_requires_path_source_target`
- `tests/test_package_boundaries.py::test_state_store_exposes_load_and_save_methods`
- `tests/test_package_boundaries.py::test_intended_submodules_are_part_of_public_package`

The legacy compatibility tests continue to assert against `f2gh.*`:

- `tests/test_characterization.py::test_load_state_returns_fresh_defaults_when_source_mismatches`
- `tests/test_characterization.py::test_load_state_returns_fresh_defaults_when_target_mismatches`
- `tests/test_characterization.py::test_load_state_returns_fresh_defaults_when_no_state_file`
- `tests/test_characterization.py::test_save_state_uses_os_replace_for_atomic_write`
- `tests/test_state.py::test_save_and_load_state_round_trip`

These must remain green throughout stage 01 because `f2gh.py` is not
touched in this stage.

A RED `to be added` test — `test_per_issue_checkpoint_advances_only_on_full_success`
— is referenced from stage 04. It does not block stage 01.

## 8. Implementation order

1. Add `forgejo_to_github/state.py` with `MigrationState`,
   `IssueCheckpoint`, `StateStore`, `StateLoadError`, `StateWriteError`,
   the `ACCEPTED_KEYS` constant, and the private atomic write helper.
2. Run `./scripts/run-tests.sh tests/test_state_store.py
   tests/test_package_boundaries.py`. Both must be green before the
   legacy compatibility tests are even consulted.
3. Run the full suite via `./scripts/run-tests.sh`. All pre-existing
   tests must remain green.
4. Stop and report.

## 9. Verification commands

```bash
./scripts/run-tests.sh tests/test_state_store.py
./scripts/run-tests.sh tests/test_package_boundaries.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/state.py
```

`mypy forgejo_to_github/state.py` is informational; the existing module
is not type-checked, but the new module should pass cleanly because it
uses standard library types and the locked dataclass signatures.

## 10. Stop gate

The implementing agent stops and reports:

- Confirmation that `forgejo_to_github.state` exists and the public
  surface matches this spec.
- Confirmation that no `f2gh.py` symbols were modified.
- Test results for `tests/test_state_store.py`,
  `tests/test_package_boundaries.py`, and the full suite.
- Any deviation from the locked constructor or save signatures, even
  in argument order, with justification.

The user reviews before stage 02 begins.

## 11. Out of scope

- Editing `f2gh.py` to call the new `StateStore`. That is stage 06.
- The `MigrationResult` dataclass. That is stage 04.
- Replacing `argparse` parsing or wiring the orchestrator. That is
  stage 06.
- Any change to `tests/test_state.py` or
  `tests/test_characterization.py`. These tests are pinned to the
  legacy functions and must not be weakened.
- Adding a CLI flag to migrate state file format versions. Not in this
  plan.
- Persisting `closed` or comment-progress per issue. See §3.1; this is
  a deliberate simplification approved at spec review.
