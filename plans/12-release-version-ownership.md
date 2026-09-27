# Plan 12 — `release` does not own the version string

**Status:** spec complete and approved; implementation not started.

**Contract change to:** `submodules/repo-mgmt-scripts/src/issue-branch`
**Amends:** `plans/09-issue-branch-workflow.md` (see §5)
**References:** GitHub issue #16 is unrelated. No issue is filed — the change
lands in the shared `repo-mgmt-scripts` submodule, which has no per-consumer
issue tracker.

---

## 1. Problem

`issue-branch release` both computes a version and applies it, which produces
two silent-wrong-version paths.

**The reported bug.** `bump_core` strips a development suffix and *then*
increments, so it reads `1.4.0.dev0` as "1.4.0 is released, move past it":

| command | from `1.4.0.dev0` | correct? |
|---|---|---|
| `finish` | `1.4.0` | yes |
| `release` (defaults to patch) | `1.4.1` | no — skips the reserved version |
| `release --minor` | `1.5.0` | no — skips the reserved version |
| `release --no-bump` | `1.4.0` | yes, but leaves the file at `1.4.0.dev0` while tagging `v1.4.0` |

**The structural bug.** After any `finish`, the base branch sits on the
already-released version, and the release version has its changelog section
behind it. A bump level was the only thing that could move it forward, so
`release` had to own the version. That coupling is what this plan removes.

**Why the fixture proves it.** `tests/test_issue_branch`'s `make_project`
builds a project at `1.3.0` whose changelog already contains
`## [1.3.0] - 2026-09-17` — a hardcoded heredoc, not this repository's
changelog. That is exactly the state of any real base branch after a release.

## 2. Design

**`release` mutates nothing.** It reads the version, promotes the changelog
with it, commits, and tags. `--major`, `--minor`, `--patch`, and `--no-bump`
are removed.

**Four subcommands may mutate the version string:**

| subcommand | runs on | mutation | role |
|---|---|---|---|
| `create` | base | bump by branch type (feature→minor, else patch) + dev suffix | **normal** — start |
| `finish` | issue branch | strip to the reserved core version | **normal** — end |
| `resume` | base | re-derive `create`'s value, **only if it differs** | **valve** — repair |
| `bump-version` | issue branch | re-level, keeping the dev suffix | **valve** — correct scope |

**Invariant.** The normal path has exactly two mutation points. A valve can
only restore or re-level a version the normal path would have produced; neither
invents a version the normal path could not. No subcommand writes a version
without either an explicit level or a development suffix to strip.

**Accepted cost.** Work committed straight to the base branch has no
subcommand that can number it, so it takes a documented manual step: edit
`__about__.py` to the new version, then run `release` for promote/commit/tag.
G2 is what makes forgetting that step loud instead of silent.

## 3. Behavior changes

| | change | why |
|---|---|---|
| **G1** | `release` refuses a dev/local-suffixed version, naming `finish` and `bump-version` | fixes the reported bug; `release` can no longer be pointed at a version it would misread |
| **G2** | `release` refuses when the current version is already tagged | a forgotten manual bump otherwise produces a duplicate `## [x.y.z]` heading, then `tag_release` (:245-249) reports "already tagged", returns `SUCCESS`, and exits 0 — a silent no-op release |
| **G3** | `resume` preserves an existing dev marker for its own branch; recomputes only when the version is missing, foreign, or unparseable | today `create feature 6` → `bump-version --minor` (`1.5.0.dev1+…`) → `resume feature 6` re-derives from the base branch's `1.3.0` and **reverts the correction to `1.4.0.dev1+…`**. The conditional write is necessary but not sufficient. |

G2 is implemented in `cmd_release`, **not** in `tag_release`: `finish`'s
post-merge recovery already short-circuits at :480-483, and making
`tag_release` itself fatal would change `finish` behavior.

## 4. Implementation

### `submodules/repo-mgmt-scripts/src/issue-branch`

- `version_is_development <version>` — matches `.devN` or `+local`. New
  predicate; `core_version` strips and so cannot detect. Issue-branch-local,
  not `functions.sh` (which holds only helpers shared across scripts).
- `cmd_release`:
  - drop the `--major|--minor|--patch|--no-bump` parser arms (:538-541) and
    the whole version block (:557-565); replace with `ib_final="$(current_version)"`
  - remove the now-dead `ib_level` and `ib_no_bump` locals and their `unset`
  - guard order: `require_base_branch` → `require_clean_tree` → G1 → G2, so
    `test_release_rejects_issue_branch` still gets the branch error rather
    than a version error
- `cmd_resume`: G3 logic.
- Header comment (:11) and `usage()` (:281) drop the removed flags.
- `bump_core`'s dev-suffix handling is **left as-is**. It is correct for a
  final version (`1.3.0` + patch → `1.3.1`), which is what `create`,
  `resume`, and `bump-version` operate on. G1 is what removes the
  dev-suffixed input from `release` entirely.

### POSIX `sh` constraints

No `local`, no `[[ ]]`, no arrays. Privates are `_`-prefixed with `unset`
after use. All exits go through `quit`.

## 5. Tests — `tests/test_issue_branch`

- **Delete** `test_release_bumps_and_tags` (:559-569) — its subject no longer exists.
- **Rewrite** `test_release_no_bump_tags_current_version` (:571-578) as
  `test_release_leaves_version_and_tags_it`: invoke with no flags; assert the
  version file still reads `1.3.0`, the tag is `v1.3.0`, and promotion
  actually ran — a **new dated heading** plus an emptied `[Unreleased]`.
  Asserting `## [1.3.0] - 2026-` would be **vacuous**: the fixture already
  contains that string at :157 before `release` runs.
- `test_release_rejects_untracked_files` (:592): `assert_absent_tag` `v1.3.1` → `v1.3.0`, since `release` no longer bumps.
- Drop the filler `--patch` from :583, :590, :600, :607.
- **New** G1 test: write a dev version directly onto `main` (overwrite
  `sample/__about__.py`, commit), then `release` → exit 1, no tag, file
  unchanged. The dev guard is unreachable from an issue branch, because
  `require_base_branch` fires first.
- **New** G2 test: pre-create tag `v1.3.0`, then `release` → exit 1, and no
  duplicate `## [1.3.0]` heading.
- **New** G3 test: `create feature 6 …` → `bump-version --minor` → `resume feature 6 …` → version is still `1.5.0.dev1+issue-6-…`.
- Update the `run_test` registry for the delete and the three additions.

## 6. Documentation

- `plans/09-issue-branch-workflow.md` — its `release` spec line (:140) and
  test list (:222) name behavior this plan removes. Plan 12 carries the new
  contract; plan 09 gets a forward-pointer rather than a rewrite, since it is
  a completed record. Plan 09 is also marked complete while sitting in
  `plans/` instead of `plans/archive/` — resolve that in the same pass.
- This repository's `AGENTS.md` — the `release` paragraph added earlier
  claims the levels skip a version, which is **wrong under this design**.
  Replace it, and state the four mutation sites with the normal/valve split.
- `submodules/repo-mgmt-scripts/README.md` — no change; it documents no flags.
- The submodule keeps no changelog of its own.

## 7. Breaking change

`release` loses four flags. `repo-mgmt-scripts` is a public repository
consumed as a submodule or via `./install`, so any consumer scripting
`issue-branch release --minor|--patch|--no-bump` breaks. Flagged because the
submodule has no changelog convention to record it in.

## 8. Verification

1. `tests/test_issue_branch` (self-contained shell harness, 666 lines).
2. `pre-commit run --all-files` — shellcheck, extension policy, YAML/JSON.
3. Manually confirm the reported bug is closed: from `1.4.0.dev0`,
   `release --minor` is no longer reachable, and `release` refuses.

## 9. Delivery

Two repositories, two commits, two pushes — the submodule first, then the
updated gitlink here. Both require the user's explicit direction; this plan
does not authorize either push.
