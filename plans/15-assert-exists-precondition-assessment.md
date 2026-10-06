# Assessment — `assert_exists` record-and-return vs. precondition abort (RED, plan 15)

**Date:** 2026-10-01 · **Context:** RED work is landed, uncommitted (`M tests/test_issue_branch`); the premature `src/write_about_version.py` has been removed (absent, as RED requires). The implementing agent surfaced C6: the two new 2.6 needles fail on interpreter noise, and proposed a `require_exists` precondition helper in a plan amendment. This is my independent verification of that assessment, done statically against the landed diff (no suite run needed — the claims are checkable by inspection).

## Verdict on the agent's assessment

**The analysis is correct; the fix direction and process call are right; the accounting numbers are wrong in both directions, and they expose a scope decision the amendment must make explicitly.**

## What is confirmed correct

- **`assert_exists` is record-and-return by design.** It matches the harness's uniform `assert_*` contract — every helper increments the counter and returns; nothing aborts the run (harness comment: "Report each test independently instead of aborting the run"). Its docstring (test file :167-170) states its original purpose: anti-vacuity — "Without this, a missing script makes the invocation exit nonzero, which reads as a pass."
- **The precondition positioning is the wrong shape.** In the six unit tests, `assert_exists` acts as a precondition ("the helper must be present before we test its behaviour"). A precondition that only records lets the body proceed against the absent dependency. Statically verified for the two 2.6 tests:
  - `python3 "$SRC_DIR/write_about_version.py"` on the absent file exits 2 with `can't open file …` noise
  - the new needles (`no __version__ assignment found` / `cannot parse`) **fail on that interpreter noise**, misattributing a dependency problem to the contract assertion
  - and `assert_ne "0" "$_status"` **passes vacuously** on it — a missing script's exit 2 reads as "rejects." That is the exact vacuity `assert_exists` was built to guard, which is the strongest argument that the guard must abort rather than record.
- **The proposed fix shape is right and has precedent inside the landed RED work itself.** `run_test`'s dependency guard (fail + return, never runs the body) and the new `make_project`/`rich_project` `if ! cp …; fail …; return 1` blocks are the same abort-on-precondition semantics. A `require_exists` helper with `|| return` callers fits the established pattern: **preconditions abort the test; assertions record within a viable test.**
- **Deferring to a plan amendment rather than quietly patching is the right process call.** It is a test-design decision.

## Where the agent's accounting is wrong

**Current RED-as-landed is 41 failed assertions / 13 failed tests, not 39:**

- 37 = baseline failed assertions (measured 2026-09-30, 12 failing tests)
- +2 = the two 2.6 needles (both fail on interpreter noise — verified statically)
- +2 = the new `test_create_fails_when_version_unreadable` (its `assert_status 1` and message assertion both fail: the committed corruption sails through the silently-empty `current_version`, so `create` exits 0)

The agent's "37→39" describes the pre-2.7 intermediate, not current RED.

**The fix's effect is also misstated.** The agent's *principle* — "a missing dependency yields exactly one named failure and no contract noise" — applies to **all six** unit tests, not just the two that C6 caught. Under the all-six scope, the four non-reject tests' body noise (~15 contract-attributed failures, e.g. "write script exits 0" failing because the script is absent rather than because it misbehaves) also disappears:

- all-six scope: 41 → **~24** failed assertions
- two-test scope only: 41 → **39**
- neither is the agent's "37 again"

The plan's **gates are unaffected either way** — RED gates at test level (33 pass / 13 fail, which holds), GREEN at 0 failed / exit 0. The amendment is a design + disclosure change, not a gate change.

## Scope decision the amendment must make

**Endorsement: all six unit tests, not just the two.** The four non-reject tests carry the same misattribution class; the plan-14 "accepted RED shape" comment only tolerated the body noise because record-and-return made it inevitable. `require_exists` makes it eliminable, and the agent's own principle demands it.

## Amendment content

1. **Scope:** all six unit tests migrate.
2. **Helper:** `require_exists <name> <path>` — calls `fail` and returns nonzero when absent; returns 0 when present. Callers: `require_exists … || return`. Mirrors `run_test` and the fixture's `if ! cp … return 1` precedent.
3. **Remove `assert_exists`** after migration — it would have zero callers (dead code in the harness otherwise).
4. **Sweep the plan's `assert_exists` references:** §2.5's conditional-copy comment and the §4.3 matrix row ("6 unit tests fail via `assert_exists`" → `require_exists`).
5. **Accounting disclosure:** state the real assertion-level numbers if any are stated — RED 41 → ~24 with the all-six scope.

**Unchanged:** the 2.6 needles themselves stay as they are — they are GREEN discriminators; they simply do not run while the guard fails.

---

# Revision review — amended plan 15 (2026-10-01)

Appended 2026-10-01 after the plan amendment that adopted this assessment's design. Re-verifies the amended plan's [the `assert_exists` precondition fix section] against the actual shell semantics. **Nothing above is rewritten — this section corrects it where needed.**

## Correction to this assessment's accounting

**The amended plan's "40" is right; this assessment's "41" was wrong.** The claim above that the new [the `current_version` propagation] test contributes +2 failures at RED is the error: its needle (`Unable to read version from`) **passes vacuously at RED**. Mechanism, traced:

1. `current_version` [at HEAD] runs its `quit` inside the command substitution — `quit`'s `echo` goes to the subshell's stdout, so the command substitution **captures the error text**, not empty: `$(current_version)` = `Unable to read version from sample/__about__.py`.
2. That text is `bump_core`'s first argument in the nested call (`ib_base="$(bump_core "$(current_version)" …)"`); `cut -d. -f1` keeps everything before the first dot of the error message; the arithmetic operand (`py`) is an unset variable name in arithmetic context and evaluates to 0.
3. The result — `"Unable to read version from sample/__about__.1.0"` — flows through `dev_version` into `write_version` and into `create`'s `Created … with version <garbage>` echo, which lands on stdout.
4. `assert_contains` checks substring presence, and the substring is present — in the scattered fragments of a garbage version string, not as an error report.

So the needle passes at RED for the wrong reason, the new test contributes +1 failure (exit status only), and RED-as-landed is 37 + 2 + 1 = **40**, exactly as the plan states. This assessment's "double-count" rebuttal was itself the error. (The same rooted error lives in one step of "What is confirmed correct" above — "the silently-empty `current_version`" — and in the audit's Context Notes memory: `current_version` never returns empty at HEAD; it returns the error text as the version.)

## Confirmed correct in the amendment

- **Design:** `require_exists <name> <path>` — records `fail`, returns nonzero when absent; callers `require_exists … || return` from the test function. Mirrors `run_test`'s dependency guard. Correct.
- **Scope:** all six unit tests, with the rationale stated. Correct, and matches this assessment's endorsement.
- **Removal of `assert_exists`** after migration (zero callers, docstring included). Correct.
- **The fixture-cp "weaker sibling" distinction** — `return` there exits only the fixture function, not the calling test, so it is not the right shape for preconditions. Correct and a valuable clarification this assessment did not make.
- **Reference sweeps** (test-file fixture comments, plan's conditional-copy comment, matrix row) — present and correct.
- **2.6 needles:** dormant at RED once the guard aborts, load-bearing at GREEN. Correct.

## Findings (disclosure-level; no gate or fix impact)

1. **The needle's RED pass is vacuous and undisclosed.** The amendment says the needle "passes at RED per the propagation test's disclosure" — but that disclosure covers only the exit-status failure ("the assertion fails for the contract reason"); it does not mention the needle. And the needle's pass is *vacuous*: the error text appears as ingredients of a garbage version string, not as an error report from `create`. At GREEN, the 3.4 split delivers the message to the call site's `quit`, `create` exits 1 with the message on stdout, and the needle passes legitimately. Red→green transition is sound; the vacuous RED pass should be stated alongside the accounting, not left implicit.
2. **Mechanism misstatement in the propagation test's RED-shape note.** "`bump_core ""` yields `.1.0` (empty arithmetic operands evaluate to 0)" — wrong input: `current_version` at HEAD does not return empty (see Correction above); it returns the error text. `bump_core` receives that text and yields `"Unable to read version from sample/__about__.1.0"`, not `.1.0`. The conclusion (`create` exits 0) is unaffected, but the mechanism as written describes an input that never occurs.

## Status

Design and scope: adopted correctly. Accounting: plan right, assessment wrong, now corrected above. Outstanding: the two disclosure-level findings above, for whoever revises the propagation test's RED-shape note and the amendment's accounting paragraph.
