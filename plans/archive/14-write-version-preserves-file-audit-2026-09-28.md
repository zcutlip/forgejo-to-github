# PLAN AUDIT — plans/14-write-version-preserves-file.md

## Resolution — closed 2026-10-01

All eleven findings are disposed of. None required reopening RED; the spec
adopted every finding in the plan-14 revision (`d4fafb9`), and plan 15 now
carries the implementation (`plans/15-repo-mgmt-scripts-defect-remediation.md`,
which cites plan 14 as the design reference). Plan 14 and this audit are
archived together; plan 15's citations point at the archive paths.

| # | Disposition |
|---|---|
| 1 | **Spec amended** — plan 15 §2.5: `make_project`/`rich_project` copy `write_about_version.py`; at GREEN the file exists and the copy proceeds. |
| 2 | **Spec amended** — missing-assignment coverage scoped to unit level only (plan 14 §5, as revised); no subcommand-level test. |
| 3 | **User-owned** — main-repo `scripts/write_about_version.py` symlink; named in plan 15 §5 delivery steps. |
| 4 | **User-owned** — submodule pointer bump; named in plan 15 §5 delivery steps. |
| 5 | **Spec amended** — the incorrect `plans/archive/13` Finding-1 citation dropped; the bare `printf` fixture stands as self-evident evidence. |
| 6 | **Spec amended** — naming/mode basis is the sibling precedent (`read_about_version.py`, mode 755), not the pre-commit hooks; hooks are `types: [shell, ...]` and do not inspect `.py` files. |
| 7 | **Spec amended** — splice pinned to text-level value-span replacement (`ast.get_source_segment` / `lineno`+`col_offset` spans); whole-tree `ast.unparse` explicitly rejected. |
| 8 | **Spec amended** — `rich_project` pinned to the plain `__version__ = "..."` form; helpers (`assert_version`, `current_version_of`, `git show` assertion) are plain-form `sed` only. |
| 9 | **Spec amended** — `resume` included in preservation coverage; all four mutating subcommands asserted. |
| 10 | **Spec amended** — replacement pinned to double-quoted literal (not `repr()`'s single-quote style), keeping the rewrite invisible in diffs. |
| 11 | **Spec amended** — throwaway target populated via `./install <dir>` (plan 14 §8 step 3). Not repeated in plan 15 §4 step 4 — reachable there via plan 15's "Per plan 14" reference. |

The audit's Follow-up checklist retains its unchecked state by design; this
block is the adoption record. Finding text left as written throughout.

## Scope

- The plan spec: `plans/14-write-version-preserves-file.md` (184 lines).
- The code it targets: `submodules/repo-mgmt-scripts/src/issue-branch`,
  `src/read_about_version.py`, `src/functions.sh`, `install`,
  `tests/test_issue_branch`, `.pre-commit-config.yaml`, `AGENTS.md`,
  `src/example_project_settings.sh`.
- The downstream consumer: `forgejo_to_github/__about__.py`,
  `forgejo_to_github/about.py`, `scripts/` symlinks, submodule pointer.

## Method

- Read the plan in full; read every file it references at the cited lines.
- Verified each factual claim against the current tree (truncation at
  `issue-branch:154-162`, call sites, AST parse in `read_about_version.py`,
  bare fixture at `test_issue_branch:198`, `install:31` file list,
  `.pre-commit-config.yaml` hook types, `about.py:8` imports, 12-line
  `__about__.py`, commit `3ca0c57` diff).
- Traced the test helpers' form sensitivity and the subcommand read-before-write
  ordering by hand; no suite execution (not requested).

## Verdict

**Do not approve the plan as-is.** The problem statement and design direction
are sound and every factual claim about the current code checks out. But two
findings would sink the RED phase, two would break the outcome, and the rest
are spec-precision gaps that would surface as contract drift at RED.

---

## High-impact findings (1–4)

### 1. Fixture copy-list gap — sinks the whole suite at RED

`write_version` will invoke `python3 "$DIRNAME/write_about_version.py"`, but
`make_project` copies only `issue-branch`, `functions.sh`, and
`read_about_version.py` into the fixture (`test_issue_branch:220-222`). The
plan says rich_project "follows the existing `make_project` shape" but never
states the fixture must also copy the new script.

Without it, every existing test that runs `create` (all of them) fails with a
missing-file error from `python3`, not for the contract reason. The failure
mode is indistinguishable from a broken implementation.

**Fix (essential):** pin in the plan that `make_project` and rich_project
copy `write_about_version.py` into `scripts/` alongside the existing three.

### 2. Missing-assignment subcommand test cannot fail for the contract reason

Every mutating subcommand reads the version before writing: `create`,
`bump-version`, and `finish` all call `current_version` →
`python3 "$DIRNAME/read_about_version.py"`, which already exits 1 on a missing
assignment (`read_about_version.py:49-50`). A subcommand-level "missing
assignment" test therefore fails at the read stage, before `write_version` is
reached — and it passes against the current destructive `write_version`, which
would truncate the file and exit 0.

This violates the plan's own RED-honesty rule ("each assertion must be shown
to fail against the current destructive `write_version`"). The
missing-assignment contract is only pinneable at the unit level, which the
plan already lists.

**Fix (essential):** drop the subcommand-level missing-assignment bullet, or
explicitly scope it as a read-path guard (asserting the command aborts before
any write) rather than a write-path test.

### 3. Main repo `scripts/` symlink not mentioned — breaks the outcome

The main repo consumes the submodule through `scripts/` symlinks:
`scripts/read_about_version.py -> ../submodules/repo-mgmt-scripts/src/read_about_version.py`
already exists. After the fix, `write_version` invokes
`python3 "$DIRNAME/write_about_version.py"` where `$DIRNAME` resolves to
`scripts/`, so `scripts/write_about_version.py` must exist as a symlink.

The plan adds the script to `install`'s file list but never mentions the
main repo's symlink. Without it, the main repo's `issue-branch` breaks on the
first `write_version` call after the submodule bump.

**User-owned.** Flagged here for completeness; the user handles this.

### 4. Submodule pointer bump not mentioned — breaks the outcome

The fix lands on the submodule's `main`; the main repo's submodule pointer
(currently `e035871`) must be bumped for `forgejo-to-github` to pick it up.
The plan does not acknowledge this step.

**User-owned.** Flagged here for completeness; the user handles this.

---

## Moderate-impact findings (5–9)

### 5. Mischaracterized audit citation (plan section 2)

The plan cites "Finding 1 of `plans/archive/13-test-audit-2026-09-26.md`" as
"the shared fixture encodes a simpler shape than reality." The actual Finding 1
is the happy-path fixture releasing v1.3.0 into a changelog that already
contains a 1.3.0 heading — a version collision, not fixture-shape blindness.

An outside reader who looks up the citation will not find what the plan
describes. The bare `printf` at `test_issue_branch:198` is self-evident
evidence and needs no citation.

**Fix:** drop the citation, or describe Finding 1 accurately.

### 6. Pre-commit hook claim is wrong (plan section 4, Naming policy)

The plan says the `script-must-have-extension` / `script-must-not-have-extension`
hooks "govern this." They do not: both are `types: [shell, ...]`
(`.pre-commit-config.yaml:13-18`), so they only inspect shell files. A `.py`
helper is outside their scope entirely.

The real basis is the sibling precedent: `read_about_version.py` is a
non-executable `.py` helper, and the new script mirrors it.

**Fix:** cite the sibling precedent, not the hooks.

### 7. Splice mechanism not pinned (plan section 3)

"Leave every other byte alone" rules out whole-tree `ast.unparse` (drops
comments, normalizes quotes and spacing), but the plan only says "splice in
the new value at that node's location." An implementer could reasonably choose
`ast.unparse` and violate the byte-preservation requirement.

**Fix:** pin the mechanism — text-level replacement of the value node's
source span via `ast.get_source_segment` or the node's
`lineno`/`col_offset`/`end_lineno`/`end_col_offset`, never whole-tree unparse.

### 8. rich_project's `__version__` form not pinned (plan section 5)

The test helpers `assert_version` (`test_issue_branch:109`),
`current_version_of` (`:166`), and the `git show` assertion (`:324`) all use
`sed -n 's/^__version__ = "\(.*\)"/\1/p'` — plain form only. If rich_project
uses the annotated form (`__version__: str = "1.3.0"`), every preservation
assertion returns empty and fails for the wrong reason (helper limitation, not
contract violation).

**Fix:** pin rich_project to the plain form; the annotated form already has
its own fixture bullet.

### 9. Resume omitted from preservation coverage (plan section 5)

The plan claims "each mutating subcommand" but lists three of four — `resume`
also calls `write_version` (`issue-branch:367`). Once rich_project is pinned
to the plain form (finding 8), resume's base-version sed at `issue-branch:359`
works fine on it, so there is no technical reason to omit resume.

**Fix:** include resume in the preservation list, or state the exclusion
rationale explicitly.

---

## Low-impact findings (10–11)

### 10. Replacement quoting not pinned (plan section 3)

The splice replaces the value span with a string literal. `repr()` yields
single quotes; the existing files use double. Either satisfies the contract,
but pinning double-quote style keeps the rewrite invisible in diffs.

**Fix:** one line in the design section.

### 11. Throwaway-target setup unspecified (plan section 8, step 3)

The end-to-end verification needs the scripts present in the throwaway
project. The plan does not say whether via `install` or manual copy.

**Fix:** one line in the verification section.

---

## What checks out

- `write_version` truncation and all five call sites — verified.
- `read_about_version.py` AST parse of both forms; `functions.sh:103` invocation — verified.
- Bare fixture at `test_issue_branch:198` — verified.
- `install:31` file list — verified.
- `about.py:8` imports; 12-line annotated-form `__about__.py`; commit `3ca0c57` diff — verified.
- AGENTS.md Config Resolution and `example_project_settings.sh:12-13` doc claims — verified.
- `release` never calls `write_version` (non-mutating) — verified.

## Follow-up checklist

- [ ] Fix Finding 1 (essential): fixture copy list includes `write_about_version.py`.
- [ ] Fix Finding 2 (essential): drop or re-scope the missing-assignment subcommand test.
- [ ] Fix Finding 3 (user-owned): main repo `scripts/write_about_version.py` symlink.
- [ ] Fix Finding 4 (user-owned): submodule pointer bump.
- [ ] Fix Finding 5: drop or correct the audit citation.
- [ ] Fix Finding 6: naming-policy basis is the sibling precedent, not the hooks.
- [ ] Fix Finding 7: pin text-level value-span splice, not whole-tree unparse.
- [ ] Fix Finding 8: pin rich_project to the plain form.
- [ ] Fix Finding 9: include resume or state the exclusion rationale.
- [ ] Fix Finding 10: pin double-quote replacement style.
- [ ] Fix Finding 11: specify throwaway-target script setup.
