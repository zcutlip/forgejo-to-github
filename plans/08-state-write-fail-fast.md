# Fail fast on state-write failure

Primary GitHub issue: #15 (`bug`).

## Context

`MigrationOrchestrator` persists checkpoints through the state seam at four
`except Exception  # noqa: BLE001 — state seam is best-effort` sites in
`forgejo_to_github/migration.py`:

- `:542` — `_mark_git_pushed`, the concrete `save` after the git phase
- `:841` — `_safe_record_issue`, the concrete `save` per issue
- `:849` — `_safe_record_issue`, the legacy `record_issue` seam
- `:862` — `_safe_record_comment`

Because the catch is `Exception` rather than a persistence error, any save
failure — and any bug inside the save path — vanishes while the run reports
success. Plan 07 fixed the *cause* of the one observed incident (no parent
directory, so every write failed) and built the landing pad for this work:
`StateWriteError` propagating out of `run()` reaches `main()`'s handler and
exits `EXIT_STATE_ERROR` (3).

## Contract

- **The state seam is not best-effort.** A state-write failure propagates
  out of `run()` instead of being swallowed.
- The existing `except StateWriteError` handler in `main()` needs no new
  machinery: the run aborts, the CLI reports, exit code 3.
- **The preflight-era message must change.** The current wording claims
  "nothing was migrated" — true for a preflight refusal, false once issues
  have been created. The replacement is one message, accurate in both
  cases: it names the resolved path and the reason, states that the run
  stopped, and says the most recent work may not be recorded, so the target
  should be checked before re-running. It must keep the properties the
  item-12 tests pin: resolved path in stderr, exit 3, no traceback, and
  output distinguishable from the lock-contention message.
- **Bounded consequence, accepted:** aborting on a failed save leaves exactly
  one issue created-but-unrecorded, so a resume can duplicate it. Loud and
  one duplicate, over silent and unbounded.

## Decisions

- **All four catches go**, including the two on the legacy seam. The rule is
  uniform — "the state seam is never best-effort" — rather than split by
  seam type. Verified: no existing test makes a save raise, and no legacy
  state fake raises, so the removal changes behavior only where persistence
  actually fails.
- **Single message**, not distinct preflight/mid-run text. Distinguishing
  would need new surface on the exception (a phase marker or subtype); the
  single message says everything the operator needs.
- **The `repo_created` fix rides on this branch** under plan 07's added
  scope; it adds no save site, so it neither needs nor preempts this
  contract.

## Out of scope

- Reporter catches (`:885-930`) — a reporting hiccup must not abort a
  migration; those stay best-effort.
- Git, label, comment-fetch, comment-create, and close catches — operational
  failures with their own accumulation semantics; untouched.
- The legacy `_already_migrated` read catch (`:821`) — a *read* failure
  treated as "not migrated". Same family, but unreachable from a real run
  (the concrete path consults the loaded map); deferred, not forgotten.
- The clone-cache feature (#5) and the `version` key (#11).

## Test contract (RED stage)

Written before implementation:

1. `tests/test_orchestration.py` — a save failure at the git-push
   checkpoint propagates out of `run()`.
2. `tests/test_orchestration.py` — a save failure at an issue checkpoint
   propagates out of `run()`, **and no further issue is created**.
3. `tests/test_orchestration.py` — a legacy `record_issue` failure
   propagates out of `run()`.
4. `tests/test_orchestration.py` — a legacy `record_comment` failure
   propagates out of `run()`.
5. `tests/test_cli.py` — the corrected message still satisfies the item-12
   properties (resolved path in stderr, exit 3, no traceback,
   distinguishable from the lock message), and no longer claims nothing was
   migrated. Amend the two item-12 tests to the new wording without
   weakening what they pin.

## References

- GitHub issue #15 (`bug`)
- `plans/07-state-file-location.md` — built the `EXIT_STATE_ERROR` landing
  pad; its added-scope section covers the `repo_created` fix on the same
  branch
