# Plan 15 — repo-mgmt-scripts: vacuous-pass remediation + implementation fixes

**Status:** spec — revised after plan review (`plans/15-repo-mgmt-scripts-defect-remediation.review.md`), awaiting review.

**Contract change to:** `submodules/repo-mgmt-scripts` — `tests/test_issue_branch` (RED), `src/` (GREEN).

**References:**

- `plans/archive/14-write-version-preserves-file.md` — `write_version` truncation; plan-14 RED committed at `f74cd95`; archived 2026-10-01 (subsumed here)
- `submodules/repo-mgmt-scripts/tests/test_issue_branch.audit.md` — independent audit, 2026-09-30
- `plans/15-repo-mgmt-scripts-defect-remediation.review.md` — plan review, 2026-10-01 (11 findings, all adopted except F11 noted as harmless redundancy)

**Branch:** `main`, by explicit user direction. This deviates from the
one-branch-per-plan convention in `AGENTS.md §4`; the user owns both
repositories and directed the work onto `main` directly.

**Scope:** all known defects in `repo-mgmt-scripts`. One RED, one GREEN.

---

## 1. Defects

| # | Defect | Source |
|---|---|---|
| 1 | Tests pass vacuously when a dependency is missing — 22 of 45 | Audit findings 1–5 |
| 2 | `write_version` truncates the version file | Plan 14 |
| 3 | `current_version` swallows errors — `quit` inside `$( )` exits only the subshell; 7 call sites continue with an empty version | Audit Context Notes |

Defect 3 is the root cause of the audit's "9 contracts that succeed over a
broken dependency" subclass. Defect 2 is plan 14's reason for existing — its
12 tests at `f74cd95` fail because of it.

## 2. RED — `tests/test_issue_branch`

**2.1 Finding 1 (Critical) — dependency guards.** Extend the `COMMAND_PRESENT`
pattern to all three required scripts (`issue-branch`, `functions.sh`,
`read_about_version.py`). **Guard predicates differ by script:** `-x` for
`issue-branch` (the executed command); `-f` for `functions.sh` (a sourced
library, mode 644 by policy) and `read_about_version.py` (invoked via
`python3`, never executed). A copied `-x` check on `functions.sh` fails at
baseline and collapses the entire RED run. `run_test` fails all tests when
any is missing.

**2.2 Finding 2 (Critical) — discriminating assertions for 13 rejection
tests.** Add `assert_contains` on the guard message. Proven pattern:
`test_finish_rejects_untracked_files` asserts `notes.txt` and correctly
failed when `functions.sh` was missing. **All needles are expanded
literals** — the harness never sources `project_settings.sh`, so a needle
written with `$ISSUE_BRANCH_BASE` will not expand.

| Test | Needle |
|---|---|
| `create_rejects_invalid_type` | `Invalid type:` |
| `create_rejects_non_numeric_issue` | `Issue number must be numeric:` |
| `create_rejects_dirty_tree` | `Tree contains uncommitted modifications:` |
| `create_requires_base_branch` | `Must be on main to run this command` |
| `resume_rejects_missing_branch` | `Branch does not exist:` |
| `bump_version_rejects_base_branch` | `bump-version must run on an issue branch, not main` |
| `bump_version_requires_explicit_level` | `bump-version requires exactly one of` |
| `release_rejects_dirty_tree` | `Tree contains uncommitted modifications:` |
| `release_rejects_untracked_files` | `Tree contains untracked files:` |
| `release_rejects_empty_unreleased` | `No content under [Unreleased]` |
| `release_rejects_issue_branch` | `Must be on main to run this command` |
| `release_rejects_dev_version` (G1) | `is a development version` |
| `release_rejects_already_tagged` (G2) | `is already tagged` |

Note on `release_rejects_issue_branch`: `cmd_release` never calls
`require_issue_branch`; the guard that fires when release runs on an issue
branch is `require_base_branch` — same needle as `create_requires_base_branch`.

**2.3 Finding 3 (Medium) — status test asserts a value.**
`assert_contains "status reports current version" "Current version:"`
asserts a label, not a value; it passes with an empty version behind the
label. Assert the version value.

**2.4 Finding 4 (Minor) — remove `set -x`.** Lines 1116–1121, debug leftover
from earlier RED work.

**2.5 Finding 5 (Minor) — fixture `cp` status.** `make_project`/
`rich_project` copy 4 scripts with no status check — 40 `cp:` error lines
per RED run. Check status for the 3 required scripts (fail on error);
conditional copy for `write_about_version.py` (absent during RED; its RED
signal is `assert_exists` in the unit tests). Note: once 2.1's suite-level
guard exists, the checked `cp` calls are unreachable defense-in-depth —
the conditional copy is the part that adds real value (kills the noise).

**2.6 Plan 14 unit tests — add message assertions.**
`test_write_script_rejects_missing_assignment` and
`test_write_script_rejects_unparseable_file` currently assert only nonzero
exit + file unchanged. A script that always exits nonzero passes both. Add
`assert_contains` on the error text: `no __version__ assignment found` /
`cannot parse`. Same finding-2 principle.

**2.7 New test — `current_version` propagation.** Locks defect 3. Corrupt
`sample/__about__.py` **and commit the corruption** (`git add -A &&
git commit`) before running `create` — `cmd_create` runs `require_clean_tree`
before the version read, so an uncommitted corruption makes `create` exit 1
with `Tree contains uncommitted modifications:` and the version-read
assertion can never pass. Assert exit 1 and output contains
`Unable to read version from`. **No branch assertion** — `cmd_create`
checks out the new branch before the version read, so at GREEN the failed
`create` leaves the repo on the new branch; every other rejection test
carries a branch assertion, but this one cannot.

RED shape verified: `bump_core ""` yields `.1.0` garbage (empty arithmetic
operands evaluate to 0), so `create` exits 0 today — the assertion fails
for the contract reason.

Test count 45 → 46.

## 3. GREEN — `src/`

**3.1 `src/write_about_version.py` (new).** Per plan 14: AST-parse, locate
the first string `__version__` assignment (plain or annotated), replace only
the value's source span via text-level splice. Byte-preserve everything
else — docstring, other names, comments, quoting, trailing-newline state.
Reject missing assignment and unparseable files (nonzero, file untouched).
Double-quoted replacement. Never imports the target package.

**3.2 `src/issue-branch` `write_version`.** Replace
`printf '__version__ = "%s"\n' "$1" > "$ib_file"` with a call to
`write_about_version.py`; `quit` on nonzero status.

**3.3 `src/functions.sh` `current_version` — error contract.** `return`
instead of `quit` on failure, echoing the error to **stdout**. Contract:
stdout is the version on success, the error message on failure. Comment at
the top of the function. The three existing messages preserved verbatim
(`Can't determine project name`, `Unable to read version from
<pkg>/__about__.py`, `Unable to detect package version`).

Two scope clarifications. First, blast radius: `current_version` also has
five callers in four untested scripts (`tag:32`, `release:40`/`:61`,
`gc_about:18`, `gc_changelog:7`). Those scripts are §5-out-of-scope, but
this change alters their runtime behavior: `tag` already checks status
(its `|| quit $?` — actually improves, it currently tags `v` + empty);
`release`, `gc_about`, `gc_changelog` are unchecked and would receive the
error text as the "version" where they today receive `""`. Second, the
`Can't determine project name` path stays dead even after this change:
`project_name`'s own `quit`-inside-`$()` is subshell-swallowed and the
`| tr '-' '_'` pipeline masks its status (pipeline status is `tr`'s, always
0). Only the read-about and setup.py paths become loud.

**3.4 `src/issue-branch` — 7 call sites.**
`ib_ver="$(current_version)" || quit "$ib_ver" $?` — the captured output
*is* the `quit` message: one line, specific, propagates the subshell error.
Split the two nested `bump_core "$(current_version)"` sites (`create` :325,
`bump-version` :456) into read-then-bump; the other five are plain sites
(`resume` :365, `status` :403, `finish` recovery :488, `finish` post-rebase
:521, `release` :556).

**Cleanup before GREEN:** remove the untracked `src/write_about_version.py`
from the premature attempt. **Already done:** `install` file list (user
committed).

## 4. Verification

**Expected RED shape (gates RED):** 33 tests pass / 13 fail — the 12
plan-14 tests plus the new 2.7 test (with its corruption committed). All 13
2.2 message assertions pass at baseline because the guard messages exist at
HEAD; they are discriminators, not new failures.

1. `sh tests/test_issue_branch` at GREEN — **0 failed / exit 0** with 46/46
   tests passing. Gated on the failure count and exit status, not on the
   passed counter reading 46 (the suite counts *assertions*, not tests —
   the counter will read ~195+).
2. `pre-commit run --all-files` — shellcheck, extension policy, YAML/JSON
3. Audit empirical matrix, expectation per scenario:
   - baseline (all scripts present, `write_about_version.py` absent):
     13 tests fail — the 12 plan-14 tests plus 2.7's (committed corruption
     → `create` exits 0 today), matching the expected RED shape above
   - `issue-branch` deleted: 0/46 (guard)
   - `functions.sh` deleted: 0/46 (guard) — was 13 vacuous passes
   - `read_about_version.py` deleted: 0/46 (guard) — was 20 suites-blind
     passes
   - `write_about_version.py` deleted **at GREEN** (load-bearing then):
     6 unit tests fail via `assert_exists`; write-exercising integration
     tests fail via `write_version`'s `quit`; guard-only rejection tests
     pass legitimately (their contracts never touch `write_version`).
     Chosen over adding it to the suite-level guard: the guard would have
     to land as a test change inside GREEN, breaking the commit firewall;
   the natural failures leave no vacuous passes. Disclosed, not guarded.
4. Manual: `create` → `bump-version` → `finish` against a throwaway target
   with the real 12-line `__about__.py`; metadata survives

## 5. Out of scope

- `deletebranch`, `gc_about`, `gc_changelog`, `release`, `tag` — untested
- Main-repo `scripts/` symlink + submodule pointer bump — user-owned
