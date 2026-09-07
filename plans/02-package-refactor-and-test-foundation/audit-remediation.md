# Audit Remediation Ledger — Plan 02 Refactor

**Parent plan:** [`../`](../) (Plan 02 — package refactor and test foundation)
**Authoritative spec:** [`./refactor/00-index.md`](./refactor/00-index.md) and the
seven stage files in `./refactor/`
**Audit under remediation:** [`../../AUDIT.md`](../../AUDIT.md)
**Behavioral baseline:** `main:f2gh.py` (the pre-refactor monolith). Whenever
the refactor spec prose and `main:f2gh.py` conflict, `main:f2gh.py` wins unless
the user has explicitly approved the newer behavior as a separate design
decision.
**GitHub issue:** [#3](https://github.com/zcutlip/forgejo-to-github/issues/3)
**Predecessor plan:** [`../../01-clone-failure-followup.md`](../../01-clone-failure-followup.md)
**Predecessor issue:** [#1](https://github.com/zcutlip/forgejo-to-github/issues/1)
**Related plan (clone-cache retention):**
[`../04-retain-clone-cache.md`](../04-retain-clone-cache.md) — backs #14
**Related plan (local-clone invocation):**
[`../05-local-clone-invocation.md`](../05-local-clone-invocation.md) — backs #14
**Related plan (keyboard-interrupt handling):**
[`../03-keyboard-interrupt-handling.md`](../03-keyboard-interrupt-handling.md)

> **Status of this document.** This is a durable, full-fidelity ledger of the
> audit's disposition. It is **not** a summary. Each AUDIT.md finding has a
> corresponding row in §2. Each additional finding has a row in §3. Each
> locked decision is restated in §4. Each remediation slice is sequenced and
> scoped in §5. Backlog and struck items are itemized in §6. The traceability
> template is in §7. Step 0 notes are in §8. **No code, tests, source, or
> other specs are modified by this document.** AUDIT.md remains untouched.

---

## 1. Purpose, Scope, and Operating Principles

### 1.1 Purpose

This document holistically remediates the audit findings recorded in
`AUDIT.md`. It is the post-audit remediation plan for Plan 02
(`plans/02-package-refactor-and-test-foundation/`), which staged the
monolithic `f2gh.py` script into a multi-file package with seam-driven unit
tests. The audit identified production-code defects introduced by the
refactor (the tests were not audited and were reported as passing). This
ledger classifies each finding, prescribes a remediation slice, identifies
the test(s) that must lock the fixed behavior, and orders the work.

### 1.2 Scope

**In scope:**

- All 20 findings in `AUDIT.md` (§2 below).
- The additional findings identified in §3 (state-persistence gap,
  resume-truthfulness gaps, phase-order regression).
- Re-locking tests that must be re-asserted where the refactor currently
  relies on or contradicts the old `f2gh.py` behavior.
- Wiring dormant helpers and existing tested capabilities that the
  refactor failed to call.
- Restoring prompts and `--yes` bypass semantics.
- A concrete end-to-end parity test that runs the orchestrator against
  real Codeberg/GitHub payload shapes with concrete collaborator seams.

**Out of scope:**

- New feature work not required by the audit findings (no new flags, no
  per-comment persistence, no async transport, no progress bars, etc.).
- Architectural changes to the locked collaborator set
  (`00-index.md` Binding Decisions) unless §5 explicitly authorizes a
  deviation with user approval.
- `plans/03-keyboard-interrupt-handling.md`,
  `plans/04-retain-clone-cache.md`, `plans/05-local-clone-invocation.md`
  beyond the cross-references above and any dependency they create here.
- Modifications to `AUDIT.md`, `pyproject.toml`, `setup.cfg`,
  `.pre-commit-config.yaml`, or any CI configuration.

### 1.3 Behavioral baseline

`main:f2gh.py` is the behavioral baseline for the audit findings. When
the refactor's stage documents in `./refactor/` and the pre-refactor
`f2gh.py` disagree about observable behavior — for example, attribution
formatting, label extraction, prompt presence, pacing, sort order, error
translation, retry behavior — `f2gh.py` is the default reference. Refactor
spec prose, locked tests, or an implementation decision do not override
that baseline merely because they were documented during the refactor. Any
intentional deviation must be identified explicitly and approved by the
user; the ledger must record the decision and its rationale. Examples already
approved here include the read-only GET-capable dry run and retaining strict
state-key validation. Those are explicit design decisions, not automatic
spec exceptions.

### 1.4 Test fidelity requirement

Per AGENTS.md §3 ("TDD order"), tests are the locked contract. New tests
must use **real Codeberg and GitHub payload shapes** — that is, the
exact JSON shapes those APIs return — and the tests must inject
**concrete collaborator seams**, not generic dicts, not lambda
catch-alls. Concretely:

- Codeberg responses in tests use real Forgejo v1 shape (e.g., an issue
  listing's `comments` field is the integer count returned by Forgejo,
  not a pre-populated list of comment dicts; comments must be fetched
  by `codeberg.list_comments(issue_id)`).
- GitHub responses in tests use real REST API v3 shape (e.g., `ensure_label`
  responds 201 on create, 422 with `errors` on duplicate, etc.).
- Collaborators are Protocol-compatible fakes (or the real clients behind
  a recording `Transport`) constructed per the locked seams in
  `00-index.md`, not one-off lambdas.

### 1.5 RED → stop → GREEN → stop protocol

Each slice in §5 follows:

1. **RED.** Write the failing test first. Run it via
   `./scripts/run-tests.sh <test module>` and confirm RED with the
   documented failure.
2. **Stop.** Report RED to the user. If the RED test exposes a
   legitimate contract gap (e.g., the spec assumed a method the
   collaborator does not yet have), surface the gap and request user
   approval to amend the test before resuming.
3. **GREEN.** Implement the minimum change to turn the test green.
   Do not amend the test to match the implementation.
4. **Stop.** Report GREEN. Run the full verification matrix in
   `00-index.md` §"High-level completion criteria" and the per-slice
   verification block in §5 below. Surface any deviation from spec
   for user approval before the next slice.

The user holds the review checkpoints. No slice proceeds to the next
without explicit user approval of the GREEN stop report.

---

## 2. AUDIT.md Finding Disposition

Every AUDIT.md finding is preserved verbatim in the **Finding** column
of the table below, with the substantive claim, the location, and the
reasoning. The columns are:

- **#** — finding number from AUDIT.md.
- **Finding** — the audit claim, restated at the level of detail
  required to act on it.
- **Class** — `Regression` (introduced by the refactor) /
  `Pre-existing` (existed in `f2gh.py` already) / `Intentional design`
  (a deviation from the old behavior explicitly approved by the user).
- **Evidence / reasoning** — why we classify it this way and what
  `f2gh.py` actually does (or did).
- **Disposition** — the remediation slice that owns it (A–G), or
  "Backlog" / "Struck" with the reason.
- **Locked test** — the test function that must lock the fixed
  behavior, by existing name where present, or "to be added" with the
  exact function name to be added.

| # | Finding | Class | Evidence / reasoning | Disposition | Locked test |
|---|---------|-------|----------------------|-------------|-------------|
| 1 | **Issue bodies are not formatted with migration attribution.** Orchestrator passes `issue.get("body", "")` directly to `github.create_issue()`. Old `f2gh.py` wrapped every body with `format_issue_body(source, cb_index, author, date, body)`. Location: `forgejo_to_github/migration.py:433-434`. | Regression | `f2gh.py` calls `format_issue_body(source, cb_index, user["login"], date, body)` and passes the wrapped string to `github_create_issue`. The refactor's orchestrator skips this entirely. | **Slice C** — wire `format_issue_body` in `MigrationOrchestrator._migrate_issue` (or equivalent) before calling `github.create_issue`. The function exists in `forgejo_to_github/formatting.py` and is already covered by `tests/test_formatting.py`. | `to be added`: `test_orchestrator_wraps_issue_body_with_attribution_block` — drives `MigrationOrchestrator` with a Codeberg issue whose body is `"original text"`, asserts the call to `github.create_issue` carries a body containing the attribution marker and the original text. |
| 2 | **Comments are not fetched from Codeberg.** Orchestrator iterates `issue.get("comments") or []`; real Codeberg API returns `"comments": 3` (an integer). Old code called `fetch_codeberg_comments(source, cb_index)` per issue. Location: `forgejo_to_github/migration.py:458`. | Regression | Forgejo's `GET /api/v1/repos/{owner}/{repo}/issues` returns `"comments": <int>`, not a list. The orchestrator never calls `codeberg.list_comments(issue_id)`. | **Slice D** — in `MigrationOrchestrator.run()` issue loop, call `codeberg.list_comments(issue_id=issue["id"])` (or `codeberg.list_comments(issue_id=issue["number"])` per the stage 02 contract) and iterate the returned comments. | `to be added`: `test_orchestrator_fetches_comments_via_codeberg_client` — asserts that for an issue whose Codeberg listing returns `"comments": 3`, the orchestrator calls `codeberg.list_comments(issue_id=...)` once with the issue's id/number and iterates the returned list (not the integer count). The fake `CodebergClient` returns a configured list. |
| 3 | **Comment bodies are not formatted with attribution.** Orchestrator passes `comment.get("body", "")` directly to `github.create_comment()`. Old code wrapped every comment with `format_comment_body(author, date, body)`. Location: `forgejo_to_github/migration.py:467`. | Regression | `f2gh.py` calls `format_comment_body(user["login"], date, body)` for each comment. The refactor's orchestrator skips this. | **Slice D** — wire `format_comment_body` in the comment-creation loop. | `to be added`: `test_orchestrator_wraps_comment_bodies_with_attribution` — drives the orchestrator with a Codeberg issue and a comment whose body is `"comment text"`; asserts the `github.create_comment` call carries a body containing the attribution marker and `"comment text"`. |
| 4 | **Labels are passed as raw dicts, not names.** `labels_raw = issue.get("labels")` yields `[{"id": 1, "name": "bug", "color": "f29513"}]`; `[str(lbl) for lbl in list(labels_raw)]` stringifies the dicts into `"{'id': 1, 'name': 'bug', ...}"`. Old code extracted `label["name"]`. Location: `forgejo_to_github/migration.py:435-439`. | Regression | `f2gh.py` does `labels = [lbl["name"] for lbl in issue.get("labels") or []]` and forwards that list to `github_create_issue`. The refactor stringifies the dicts. | **Slice C** — extract `[lbl["name"] for lbl in (issue.get("labels") or [])]` before passing to `github.create_issue`. Combine with #5 (ensure_label). | `to be added`: `test_orchestrator_passes_label_names_not_dicts` — drives with a Codeberg issue whose `labels` is `[{"id": 1, "name": "bug", "color": "f29513"}]`; asserts `github.create_issue` is called with `labels=["bug"]` and that no stringified dict ever reaches GitHub. |
| 5 | **No label creation/ensuring before issue creation.** Orchestrator never calls `github.ensure_label()`. Audit premise: "the new orchestrator never calls `github.ensure_label()`." Location: missing in `forgejo_to_github/migration.py`. | Premise partially incorrect → approved to fix | **Premise correction.** The audit's stated premise ("the old code called `ensure_label` for each label before creating the issue") is **incorrect** for `main:f2gh.py` as it stood at audit time. The old monolith did not contain or call `ensure_label` in the issue loop. The underlying fresh-repo label-preservation deficiency is therefore a **pre-existing** issue, not a refactor regression. **Approved fix.** The user has approved wiring the existing tested `ensure_label` capability into the orchestrator as an improvement, because the capability is already covered by `tests/test_github_client.py::test_ensure_label_posts_payload_when_label_missing` and `tests/test_github_client.py::test_ensure_label_does_not_repost_when_label_already_exists`, and the deficiency produces real failures on fresh target repos. | **Slice C** — wire `github.ensure_label(name, color)` for each label in `MigrationOrchestrator._migrate_issue` (or equivalent) **before** calling `github.create_issue`. Use the default color `DEFAULT_LABEL_COLOR = "ededed"` per `04-orchestrator.md` §3.11 when the source label lacks a color. | `to be added`: `test_orchestrator_ensures_each_label_before_creating_issue` — drives with two labels `bug` and `feature`; asserts `ensure_label("bug", ...)` and `ensure_label("feature", ...)` are each called before `create_issue`, and that `create_issue` is called exactly once. Also asserts `DEFAULT_LABEL_COLOR` is substituted when the source label has no `color`. |
| 6 | **No repository creation or description update.** Orchestrator never calls `github.create_repository()`. Spec (`04-orchestrator.md` §3.2 step 2) requires this. Location: missing. | Regression (root cause) | `f2gh.py` checks `if check_target_repo(target) is None`, prompts, resolves a description (explicit `--description` wins; else fetches the Codeberg description; on HTTP failure falls back to `"Migrated from Codeberg"`), and calls `create_github_repo(target, description, public)` **with the description folded into the create payload**. It **never** PATCHes the description after creation — the only PATCH in `f2gh.py` is `close_github_issue`. When the target already exists, it does nothing description-related even if `--description` is supplied. The refactor skips repo creation entirely; this is the root cause of the "migrate-into-non-existent-repo crashes" failure. **Spec deviation noted:** `04-orchestrator.md` §3.8 rule 1 says the orchestrator "calls `github.update_repository_description(...)`" when `repo.description` is non-empty — that PATCH-after-create design was introduced during the spec-pass consolidation and was **never** old behavior. Per the governing baseline rule (§1.3), Slice A folds the description into `create_repository(...)` and does **not** call `update_repository_description` from the orchestrator. The client method remains unit-tested (`test_github_update_repository_description_patches_description`) but is not invoked by the orchestrator. | **Slice A** — restore the pre-flight phase per old behavior. Order: check `github.check_repository_exists(target)`; if missing, resolve the description (explicit `repo.description` wins; else fetch via `codeberg.get_repository_description()`; on HTTP failure fall back to `"Migrated from Codeberg"`); call `github.create_repository(name, description, public)` with the description in the create payload. Do **not** call `update_repository_description` from the orchestrator. Existing targets: no description action. | Existing `tests/test_repository_description.py` (per `00-index.md` traceability table row "02-api-clients (description behavior)"). Plus `to be added`: `test_orchestrator_creates_target_when_missing` — drives with a fake `GitHubClient` whose `check_repository_exists` returns `None`; asserts `create_repository` is called with the resolved description in the payload. Plus `to be added`: `test_orchestrator_does_not_touch_description_when_target_exists` — drives with a target that already exists and a non-empty `--description`; asserts `update_repository_description` is NOT called. |
| 7 | **No interactive confirmation prompts.** Old code prompted before repo creation and before migrating into a repo with existing issues. `--yes` flag skipped prompts. New CLI has no prompts; `--yes` is a no-op. Location: `f2gh.py` (missing). | Regression | `f2gh.py` has `confirm(prompt, *, yes)` calls before destructive actions; `--yes` causes `confirm` to return `True` without prompting. The refactor removed both the prompts and the bypass. | **Slice A** — restore both prompts in `f2gh.py` (or wherever the CLI seam lives post-stage 06) with the `--yes` bypass. Locked decision §4 below: deny-by-default `prompter=None` injected into the orchestrator; CLI supplies a stdin confirmer. The orchestrator is responsible for **calling** the prompter at the same two pre-pivot points as old `f2gh.py`; the prompter's behavior (interactive vs. auto-yes) is the collaborator's responsibility. | `to be added`: `test_orchestrator_prompts_before_creating_target` — drives with a `prompter` whose record shows it was called once before repo creation; the prompter returns `True`. Plus `to be added`: `test_orchestrator_skips_prompts_when_yes_flag_set` — drives with `repo.yes=True`; asserts the prompter is **not** called and creation proceeds. Plus `to be added`: `test_orchestrator_prompts_when_target_has_existing_issues` — drives with a target that exists and reports N open issues; asserts the prompter is called once with a confirmation message referencing the existing issues. Plus `to be added`: `test_orchestrator_aborts_when_prompter_returns_false` — drives with a prompter returning `False`; asserts no `create_repository` or `create_issue` call and that the result surfaces an abort path (proposed `result.aborted = True` flag — see §4). |
| 8 | **No rate limiting between API calls.** Old code had `time.sleep(0.3)` between issue creation, comment creation, and issue close. Orchestrator fires calls as fast as possible. Location: missing. | Regression | `f2gh.py` calls `time.sleep(0.3)` after `github_create_issue`, `github_create_comment`, and `github_close_issue`. The refactor omits all three. This is the secondary-rate-limit (GitHub-abuse-triggered 429/403) trigger. | **Slice B** — restore the 0.3s sleep between consecutive issue-mutation calls. Lock the value as a module constant (`_ISSUE_MUTATION_PAUSE_SECONDS = 0.3`) so future jitter/backoff work can extend it. Also restore `f2gh.py`'s proactive low-remaining throttle (`gh_request` sleeps 2 s whenever `X-RateLimit-Remaining` is present and below 10, before the request is sent). | `to be added`: `test_orchestrator_pauses_between_issue_mutation_calls` — drives with three issues; asserts the elapsed time between successive `create_issue` calls is at least `0.3 - epsilon` and uses a clock injected through the orchestrator (or, where the orchestrator does not own a clock, asserts a `time.sleep` mock was called with the documented value). Plus `to be added`: `test_github_client_proactive_sleep_when_remaining_low` — responds with `X-RateLimit-Remaining: 3`; asserts a 2-second sleep precedes the request and the request still completes. |
| 9 | **No issue sorting by creation date.** Old code sorted issues by `created_at` before migrating. Orchestrator processes in API pagination order. Location: missing. | Regression | `f2gh.py` does `codeberg_issues.sort(key=lambda i: i["created_at"])` before iteration. The refactor omits the sort. | **Slice C** — sort the discovered issues by `created_at` (ascending) before iterating, using the same key the old code used. | `to be added`: `test_orchestrator_migrates_issues_in_creation_date_order` — drives with issues `[#3 created later, #1 created earlier, #2 created middle]`; asserts the order of `create_issue` calls is `[#1, #2, #3]`. |
| 10 | **`issues_attempted` incremented before resume check.** `result.issues_attempted += 1` happens before `self._already_migrated(source_number)`. Already-migrated issues are counted as "attempted". Location: `forgejo_to_github/migration.py:419-426`. | Regression (reporting) | The counter is incremented before the resume guard. On a resumed run, checkpointed issues are counted as "attempted" even though no work was done for them. The audit is correct; this overstates `issues_attempted` and misleads the reporter's truthfulness assertions. | **Slice E** — move the `issues_attempted += 1` increment to **after** the resume guard; only count issues that the orchestrator actually begins work on. (See `04-orchestrator.md` §3.4 counter table: `issues_attempted` is incremented when "S1 begins for this issue" — S1 does not begin for resumed issues; the current code violates the spec's own contract.) | Existing `tests/test_migration_reporting.py::test_successful_issues_are_checkpointed_and_resume_filters_them` should be augmented (with user approval per `00-index.md` "no test weakening") to also assert `result.issues_attempted` excludes resumed issues. Plus `to be added`: `test_issues_attempted_excludes_resumed_issues` — drives with state containing three checkpointed issues and three new ones; asserts `result.issues_attempted == 3` and `result.issues_succeeded == 3`. |
| 11 | **`comments_attempted` not incremented for skipped malformed comments.** A comment with a non-integer index is silently `continue`d without incrementing `comments_attempted`. Location: `forgejo_to_github/migration.py:460-464`. | Minor observability issue | The skip is silent. Per `04-orchestrator.md` §3.4 the counter for `comments_attempted` is incremented "S3 begins for each comment" — but if a malformed comment is filtered out before S3, the counter should still record the attempt-as-skipped, otherwise the reporter and operator cannot tell that something was filtered. This is a free-rider finding: it travels with #2/#3 but is independently observable. | **Slice D** — when a malformed comment is filtered, increment `comments_attempted` so the count matches what the operator sees in the source. If the comment body is empty or the user/index is unusable, log a one-line warning and continue. | `to be added`: `test_orchestrator_counts_skipped_malformed_comments` — drives with a comment list including one with a malformed `id`; asserts `result.comments_attempted` includes the skipped comment and a warning is emitted to the reporter. |
| 12 | **`_request_with_rate_limit_retry` only retries 429, not 403 with `X-RateLimit-Remaining: 0`.** Code only retries 429. Location: `forgejo_to_github/github.py:408-431`. | Regression vs. old `retry-both` behavior | `f2gh.py` retries both 429 and 403-with-zero-remaining up to 3 total attempts (`max_retries=3`, `range(3)`) with header-driven delay and additive jitter (`retry_after + random.uniform(0, 2)`). The refactor narrowed to 429-only and fail-fasts on 403. The stage-02 spec amendment to `02-api-clients.md` §3.3 (which codified 403-raises-immediately to match the locked test) was itself the regression creeping into the spec; Slice B reverts that amendment. The test `test_403_with_zero_rate_limit_remaining_raises_rate_limit_error` in `tests/test_github_client.py` enshrines the regression. **RED test must be re-locked.** | **Slice B** — restore retry-both behavior: retry on 429 or 403-with-`X-RateLimit-Remaining: 0` up to 3 total attempts (`_MAX_ATTEMPTS = 3`); honor `Retry-After` (or `X-RateLimit-Reset`) delay with additive jitter (`delay + random.uniform(0, _JITTER_SECONDS)` where `_JITTER_SECONDS = 1.0`); on exhaustion raise `GitHubRateLimitError`. Re-lock stage-02 test `test_403_with_zero_rate_limit_remaining_retries_then_raises` (renamed from the current `test_403_with_zero_rate_limit_remaining_raises_rate_limit_error`, with user approval per `00-index.md` "no test weakening"). Revert the `02-api-clients.md` §3.3 amendment. | Re-locked `test_403_with_zero_rate_limit_remaining_retries_then_raises` (renamed from the existing `test_403_with_zero_rate_limit_remaining_raises_rate_limit_error`) — drives the GitHub client with three sequential 403-with-zero-remaining responses; asserts the retry loop invokes the request three times total (one initial + two retries), honors `Retry-After`, and on the third 403 raises `GitHubRateLimitError`. Plus `test_rate_limit_429_retries_then_raises_with_retry_after_jitter` (existing `test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error` augmented to assert jitter) — also re-locked in Slice B. |
| 13 | **`_sleep_for_rate_limit` can sleep for a very long time.** Far-future `X-RateLimit-Reset` produces multi-hour sleeps without cap. Location: `forgejo_to_github/github.py:438-455`. | Genuine pre-existing bug | `f2gh.py` has the same unbounded sleep. The bug pre-dates the refactor. The audit classifies it Medium. | **Backlog.** Track in §6 backlog list. Do **not** fix in this remediation. The fix (cap retry-after at e.g. 60s, plus jitter) is appropriate as a follow-up plan or stage 02 amendment, but not as part of audit remediation, which is scoped to regressions and confirmed defects from the audit's findings list. | n/a (out of scope) |
| 14 | **`CodebergClient._paginate` has no page limit safeguard.** Loop runs until an empty page; misbehaving API could loop forever. Location: `forgejo_to_github/codeberg.py:337-406`. | Genuine pre-existing bug | `f2gh.py` pagination has the same shape; the bug pre-dates the refactor. The audit classifies it Medium. | **Backlog.** Track in §6 backlog list. Cross-reference `04-retain-clone-cache.md` and `05-local-clone-invocation.md` because those plans will exercise pagination against cached/local clones and a runaway loop would compound. | n/a (out of scope) |
| 15 | **`StateStore.load` rejects unknown top-level keys.** Any unknown key causes `StateLoadError`. Forward compatibility broken. Location: `forgejo_to_github/state.py:245-251`. | Intentional design | The refactor made validation stricter than the old `.get()`-based loader. The user explicitly approved retaining strict validation for now because compatibility is not currently a product requirement. Forward compatibility is a future concern, not a current bug. | **Struck from current remediation.** Recorded in §6 for traceability. If/when schema evolution becomes a product requirement, revisit this policy under explicit user approval. | n/a |
| 16 | **`StateStore.save` does not preserve `version` key.** `save()` writes no `version` key. The file is effectively legacy format. Location: `forgejo_to_github/state.py:337-344`. | Not a current bug | The old `f2gh.py` also wrote no version key. The audit classifies it Medium, but the absence of a version key is not a current defect — both old and new code produce state files without one. A `version` key is a future-schema-evolution enabler, not a current behavioral requirement. | **Struck from current remediation.** Recorded in §6 backlog for future schema work; not part of this audit remediation. | n/a |
| 17 | **`GitMirror._tempdir_prefix` uses `rstrip(".git")` which strips character set, not suffix.** For `tagging.git`, `rstrip(".git")` strips `{'.','g','i','t'}` characters, mangling the prefix. Location: `forgejo_to_github/git.py:676-687`. | Refactor-introduced bug | The refactor introduced this; `f2gh.py` does not have a `rstrip` call of this kind. The fix is to use `str.removesuffix(".git")`. | **Slice F** — replace `rstrip(".git")` with `removesuffix(".git")`. Surface test must cover a repo name ending in `g`, `i`, `t`, or `.` to prove the bug existed. | `to be added`: `test_git_mirror_tempdir_prefix_uses_removesuffix_not_rstrip` — drives with the source URL `https://codeberg.org/owner/tagging.git`; asserts the tempdir prefix is `tagging` (not mangled) and is constructed from the suffix-stripped form. |
| 18 | **`CodebergClient.get_issue` has a typo in error message.** `"Codehub rate limit exceeded"` instead of `"Codeberg rate limit exceeded"`. Location: `forgejo_to_github/codeberg.py:249`. | Cosmetic | Trivial; appears in user-facing error output. | **Slice F** — change `"Codehub"` to `"Codeberg"`. | `to be added`: `test_codeberg_get_issue_rate_limit_message_says_codeberg` — drives with a 429 response; asserts the raised `CodebergRateLimitError`'s message contains `"Codeberg"` and not `"Codehub"`. |
| 19 | **`Reporter.issue_failed` signature mismatch with orchestrator.** Spec defines `issue_failed(source_number, kind, message)` but orchestrator calls it as `issue_failed(source_number, message)`. Reporter handles this by treating `kind` as message when `message` is None and defaulting `kind` to `"issue_create"`. Comment and close failures are mislabeled. Location: `forgejo_to_github/reporting.py:94-110`. | Regression (fragile live-reporting) | The reporter is doing type-shape inference to recover from the wrong call; this is exactly the kind of "fragile" wiring that fails under review. The audit is correct: the orchestrator should call the reporter with `(source_number, kind, message)`, and the reporter's existing `IssueFailure.kind` semantics from `04-orchestrator.md` §3.9 (`"issue_create"`, `"comment"`, `"close_failed"`, `"label_create"`) should flow through cleanly. | **Slice C** — fix the orchestrator to call `reporter.issue_failed(source_number, kind, message)` per the spec. The reporter's parameter-shuffling fallback becomes dead code. (Slice E also picks up the broader "kind" surface if it is needed by the truthfulness counts — see additional findings §3.) | `to be added`: `test_reporter_issue_failed_receives_distinct_kind_per_failure_step` — drives the orchestrator with a Codeberg issue that fails to create, then a comment that fails, then a close that fails; asserts the reporter receives three `issue_failed` calls with `kind` values `"issue_create"`, `"comment"`, and `"close_failed"` respectively (in any order). |
| 20 | **`format_issue_body` and `format_comment_body` are dead code.** Imported nowhere in production. Only exercised by characterization/formatting tests. Location: `forgejo_to_github/formatting.py`. | Regression consequence | This is the same defect as #1 and #3, surfaced from a different angle: the helpers exist, are unit-tested, but the orchestrator doesn't call them. The audit correctly classifies this as the consequence of #1 and #3. | **Slice C + Slice D** — wiring #1 and #3 automatically resolves #20 (the helpers become live code). No separate work item is required; the verification is that the Slice C/D RED tests now exercise the helpers through the orchestrator, not just through the formatter tests. | Verified by the Slice C/D tests for #1 and #3; no separate test required. |

### 2.1 Disposition summary

- **Slice A:** #6, #7.
- **Slice B:** #8, #12.
- **Slice C:** #1, #4, #5, #9, #19 (and #20 as a consequence).
- **Slice D:** #2, #3, #11 (and #20 as a consequence).
- **Slice E:** #10.
- **Slice F:** #17, #18.
- **Slice G (parity):** end-to-end coverage that exercises the fixes above through the orchestrator.
- **Backlog:** #13, #14.
- **Struck (not in current remediation):** #15, #16.

---

## 3. Additional Findings (Beyond AUDIT.md)

These are defects the audit did not enumerate but which surfaced during
classification of the audit findings and during review of the stage
specs. Each is assigned a slice, evidence, and a locked test.

### 3.1 Successful `git_pushed` state never persisted

- **Class:** Regression.
- **Evidence / reasoning:** `f2gh.py` records `state["git_pushed"] = True`
  on a successful push (so a later resume skips the entire Git phase:
  `main:673` "Git already pushed (from previous run). Skipping."). The
  refactor's `StateStore.save` **does** include a `git_pushed` field
  (`state.py:315`, `save(repo_created, git_pushed, migrated)`) and
  `MigrationState` **does** have `git_pushed: bool` (`state.py:95`).
  The defect is narrower: the orchestrator loads `_concrete_git_pushed`
  from state and persists it via the concrete save path, but **never
  sets it to `True`** after a successful push — so a resume always
  re-runs the Git phase. The state is lying-by-omission.
- **Disposition:** **Slice E** (resume truthfulness). After a successful
  push, set `_concrete_git_pushed = True` and persist via the existing
  concrete save path. On resume, if `git_pushed` is `True`, skip the
  **entire** Git phase (both clone and push, matching `main:673`) and
  record `git["clone"]="skipped"` and `git["push"]="skipped"`. Issues
  are enumerated via the Codeberg API, not the Git clone, so clone is
  not needed on resume.
- **Locked test:** `to be added`:
  `test_state_records_git_pushed_after_successful_push_and_skips_on_resume` —
  drives the orchestrator with a successful push; asserts
  `state.migrated` and `state.git_pushed` are persisted; on
  resume, asserts **both** `git["clone"]="skipped"` and
  `git["push"]="skipped"` (the entire Git phase is skipped, matching
  `main:673`; issues are enumerated via the Codeberg API, not the
  clone). Plus
  `test_state_does_not_record_git_pushed_when_push_fails` — drives
  with a failed push; asserts `state.git_pushed` is not set.

### 3.2 Resumed/skipped issues lack visible reporting

- **Class:** Regression (reporting).
- **Evidence / reasoning:** `f2gh.py` prints a resume banner
  ("Resuming migration. N issue(s) already migrated, skipping." at
  `main:611`) and an aggregate count in the final summary
  ("Issues skipped: N (already migrated)" at `main:852`). It does
  **not** print a per-issue skip line. The refactor's reporter surface
  emits neither the banner nor the aggregate count, so resumed issues
  are completely invisible. The `MigrationResult` does carry enough
  state for the reporter to compute the skip count (the number of
  `state.migrated` keys minus the number of issues the orchestrator
  entered), but the reporter itself never surfaces skip messages.
- **Disposition:** **Slice E**. Add `reporter.issue_skipped(source_number)`
  to the Reporter Protocol and call it from the orchestrator when an
  issue's `number` is in `state.migrated` before S1. The per-issue skip
  line is an **improvement over** old behavior (which only had the
  resume banner and aggregate count); it is approved as a
  truthfulness enhancement consistent with the user's stated goal of
  leaving the user with a clear understanding of state.
- **Locked test:** `to be added`:
  `test_reporter_emits_issue_skipped_message_for_resumed_issue` —
  drives with a state containing one checkpointed issue and a
  Codeberg listing that includes it; asserts the reporter's normal
  sink receives a line referencing the source number and the words
  "already migrated".

### 3.3 Phase order: old `repo creation → git → issues`; refactor currently starts with git

- **Class:** Regression (root cause for #6 + #7 surfacing).
- **Evidence / reasoning:** `f2gh.py`'s `migrate` flow is:
  (a) prompt for confirmation if needed; (b) check target exists and
  create/describe it; (c) clone + push; (d) issue migration. The
  refactor's `MigrationOrchestrator.run()` per `04-orchestrator.md`
  §3.2 currently reads as: dry-run short-circuit; pre-flight (target
  check + create); description; **git mirror**; issue migration. The
  ordering in the spec is **already correct** (pre-flight before git),
  but the implementation in `forgejo_to_github/migration.py` skips
  step 2 entirely and begins with the git phase, which is what the
  audit observes as "no repository creation." So this is not a phase
  order bug in the spec; it is the same defect as #6 surfaced at the
  level of phase-order observability. **However**, an audit reviewer
  reading only the implementation could reasonably conclude that the
  refactor intends `git → issues` (skipping pre-flight), which is why
  it appears here as an explicit additional finding.
- **Disposition:** **Slice A** (combined with #6). The fix for #6
  restores pre-flight; the result is the documented phase order
  (pre-flight → git → issues) and the audit's "refactor currently
  starts with git" observation goes away.
- **Locked test:** Covered by the Slice A test
  `test_orchestrator_creates_target_when_missing` plus an additional
  ordering assertion: `test_orchestrator_runs_preflight_before_git_phase`
  — drives with a missing target and asserts the call order is
  `create_repository` (if needed) → `git.clone()` → `codeberg.list_issues()`.

---

## 4. Locked Decisions

These decisions are **locked** for this remediation. They are not
negotiable inside any individual slice. If a slice appears to require
deviating from one of them, stop and surface the conflict.

### 4.1 Restore both prompts with `--yes` bypass (resolves #7)

- The pre-pivot points are exactly the two `confirm` calls in
  `f2gh.py`: one before repo creation (when the target does not
  exist), and one before issue migration when the target has existing
  issues.
- The prompt wording follows `f2gh.py` for behavioral parity. Minor
  editorial drift is permitted (per `00-index.md` "User-facing wording")
  but the two prompts and their defaults must remain.
- `--yes` (mapped to `Repository.yes = True`) bypasses both prompts.
- The prompter is a small Protocol on the orchestrator: a single
  callable `prompter(prompt: str, default: bool) -> bool`. The
  default prompter is `None` (deny-by-default), and the CLI supplies
  a real prompter that reads from stdin (or auto-affirms when
  `repo.yes` is `True`). The orchestrator never prompts directly; it
  only calls the injected prompter. If the prompter returns `False`,
  the orchestrator records an abort on the result
  (`MigrationResult.aborted: bool = False`) and exits the run without
  performing further mutations. The reporter surfaces the abort.

**Design recommendation (deny-by-default null prompter).** The
`MigrationOrchestrator` constructor takes a `prompter` argument that
defaults to `None`. When `None`, the orchestrator treats any prompt
as denied (so a test or alternate caller that forgets to inject a
prompter fails closed). The CLI constructs a real prompter that
inspects `repo.yes`: if `True`, return `True` without prompting;
otherwise read from stdin with the documented default. The
production wiring matches `f2gh.py`'s `confirm` behavior.

**RED design tests (explicitly marked).** The Slice A RED tests for
#7 include the following design-level assertions that the locked
test names must encode:

- `test_orchestrator_with_null_prompter_treats_prompts_as_denied` —
  drives with `prompter=None`; asserts no `create_repository` or
  `create_issue` call and `result.aborted is True`.
- `test_cli_prompter_bypasses_when_yes_flag_set` — drives the CLI
  helper that constructs the prompter with `Repository(yes=True)`;
  asserts the prompter returns `True` without reading stdin (a fake
  stdin is supplied and is asserted to be empty after the call).
- `test_cli_prompter_reads_stdin_when_yes_flag_unset` — drives the
  CLI helper with `Repository(yes=False)` and a fake stdin that
  contains `"yes\n"`; asserts the prompter returns `True` and stdin
  was consumed.

These three tests are written in RED before Slice A's GREEN work
begins, and they are part of the Slice A stop report.

### 4.2 Retry 429 and 403-with-zero-remaining up to 3 total attempts with header delay + additive jitter (resolves #12)

- Both 429 and 403-with-`X-RateLimit-Remaining: 0` are retried up to
  3 **total** attempts (`_MAX_ATTEMPTS = 3`, matching `f2gh.py`'s
  `max_retries=3` / `range(3)`): one initial request plus up to two
  retries. On the third rate-limited response, raise
  `GitHubRateLimitError`.
- Delay is `Retry-After` (seconds or HTTP-date) or
  `X-RateLimit-Reset - now` (seconds), whichever is present.
- Jitter: **additive**, matching `f2gh.py`'s
  `retry_after + random.uniform(0, 2)` and the current client's
  `_JITTER_SECONDS = 1.0`. The sleep value is
  `delay + random.uniform(0, _JITTER_SECONDS)`.
- On exhaustion, raise `GitHubRateLimitError`.
- The stage-02 test `test_403_with_zero_rate_limit_remaining_raises_rate_limit_error`
  must be **re-locked** (renamed to
  `test_403_with_zero_rate_limit_remaining_retries_then_raises`)
  with user approval per `00-index.md` "no test weakening." The
  amendment is recorded in the Slice B stop report with the reason:
  the current test enshrines the regression; reverting to old-behavior-
  faithful retry-both requires amending the test.
- The stage-02 **spec amendment** to `02-api-clients.md` §3.3 (which
  codified "403-with-zero-remaining raises immediately" to match the
  locked test) is **reverted** as part of Slice B, with user approval.
  That amendment was the regression creeping into the spec.
- Existing `test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error`
  is augmented (with user approval) to also assert additive jitter: the
  sleep value is in `[delay, delay + _JITTER_SECONDS]`.
- Also restored in this slice (Slice B scope item 2): `f2gh.py`'s
  proactive low-remaining throttle (`gh_request`, `main:82-84`) —
  sleep 2 s before dispatch whenever `X-RateLimit-Remaining` is
  present and below 10. Locked by
  `test_github_client_proactive_sleep_when_remaining_low`.

### 4.3 Keep strict state validation (resolves #15)

- `StateStore.load` continues to reject unknown top-level keys.
- Forward compatibility is not addressed in this remediation. (See §6.)
- If a `version` key is added in future schema work, the strict policy
  will be revisited under user approval at that time.

### 4.4 Wire `ensure_label` for fresh-repo label preservation (resolves #5)

- The orchestrator calls `github.ensure_label(name, color)` for each
  label on the issue before calling `github.create_issue`.
- Default color when the source label has no `color`:
  `DEFAULT_LABEL_COLOR = "ededed"` per `04-orchestrator.md` §3.11.
- `ensure_label` is idempotent — the existing
  `test_ensure_label_does_not_repost_when_label_already_exists`
  pins this — so it is safe to call on every issue.

### 4.5 Prompter collaborator design (locked for Slice A; RED design tests above)

- See §4.1. The locked signature is
  `MigrationOrchestrator(..., prompter: Callable[[str, bool], bool] | None = None)`.
  The default `None` is deny-by-default. The CLI is the only
  production caller that injects a real prompter.

### 4.6 Slice G end-to-end parity test (locked for Slice G)

- Slice G adds one explicit end-to-end parity test that drives the
  orchestrator through every fixed slice simultaneously:
  - A Codeberg issue with three comments and two labels.
  - A target repo that does not exist (triggers creation + label
    ensure + description fallback).
  - A push that succeeds (so `git_pushed` is recorded).
  - One resumed issue from a pre-populated state (so the skip
    message is emitted and `issues_attempted` excludes it).
  - One malformed comment (so `comments_attempted` includes it but
    it is filtered).
- The test asserts:
  - The body of the GitHub `create_issue` call contains the
    attribution block and the original body.
  - The body of each GitHub `create_comment` call contains the
    attribution block and the original comment body.
  - `create_issue` is called with `labels=["bug", "feature"]` and
    `ensure_label` is called twice with the same names.
  - The repo is created, then the description is set from the
    source (or from the fallback), then the Git phase runs, then
    the issues are migrated in `created_at` order.
  - `result.issues_attempted == 1` (the one non-resumed issue),
    `result.issues_succeeded == 1`, `result.comments_attempted == 4`
    (three good + one malformed), `result.comments_succeeded == 3`.
  - `state.git_pushed is True` and on resume, **both**
    `git["clone"]` and `git["push"]` are `"skipped"` (the entire
    Git phase is skipped, matching `main:673`).

---

## 5. Ordered Remediation Slices A–G

Each slice follows the RED → stop → GREEN → stop protocol from §1.5.
Each slice ends with a stop report; the user reviews before the next
slice begins.

### Slice A — Repository preparation, state, and phase order

**Resolves:** #6 (repository creation / description), #7 (prompts /
`--yes`), additional finding §3.3 (phase order).

**Scope:**

1. Restore the pre-flight phase per old behavior:
   check `github.check_repository_exists(target)`; if missing, resolve
   the description (explicit `repo.description` wins; else fetch via
   `codeberg.get_repository_description()`; on HTTP failure fall back
   to `"Migrated from Codeberg"`); call `github.create_repository(name,
   description, public)` with the description in the create payload. Do
   **not** call `update_repository_description` from the orchestrator.
   Existing targets: no description action.
2. Restore both prompts via the locked `prompter` design (§4.1).
   `Repository.yes = True` bypasses both prompts. `prompter=None`
   is deny-by-default.
3. Restore the documented phase order: pre-flight → git → issues.

**Out of scope for Slice A:** pacing (Slice B), labels (Slice C),
comments (Slice D), counters (Slice E), small fixes (Slice F).

**RED tests (all `to be added` unless noted):**

- `test_orchestrator_prompts_before_creating_target`
- `test_orchestrator_skips_prompts_when_yes_flag_set`
- `test_orchestrator_prompts_when_target_has_existing_issues`
- `test_orchestrator_aborts_when_prompter_returns_false`
- `test_orchestrator_with_null_prompter_treats_prompts_as_denied`
  (design RED test per §4.1)
- `test_cli_prompter_bypasses_when_yes_flag_set` (design RED test per §4.1)
- `test_cli_prompter_reads_stdin_when_yes_flag_unset` (design RED test per §4.1)
- `test_orchestrator_creates_target_when_missing`
- `test_orchestrator_does_not_touch_description_when_target_exists`
- `test_orchestrator_uses_fallback_description_on_source_fetch_failure`
- `test_orchestrator_runs_preflight_before_git_phase`

**GREEN work:**

- Add the `prompter` parameter to `MigrationOrchestrator.__init__`
  with the default of `None` (deny-by-default).
- Add the pre-flight phase to `MigrationOrchestrator.run()`.
- Wire `check_repository_exists` and `create_repository` through the
  existing `GitHubClient` seam (per `02-api-clients.md`).
- Add the `MigrationResult.aborted: bool = False` field per §4.1.
- Update `f2gh.py` (or its stage-06 successor) to construct the
  CLI prompter and inject it into the orchestrator.

**Verification (Slice A):**

```bash
./scripts/run-tests.sh tests/test_orchestration.py
./scripts/run-tests.sh tests/test_cli.py
./scripts/run-tests.sh tests/test_repository_description.py
./scripts/run-tests.sh tests/test_migration_reporting.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/migration.py
ruff check f2gh.py
mypy forgejo_to_github/migration.py             # informational
```

**Stop gate (Slice A):** report the diff, the new test names, the
verification outcomes, and any spec deviation (e.g., new
`MigrationResult.aborted` field) with justification.

---

### Slice B — Rate limiting and pacing

**Resolves:** #8 (pacing), #12 (retry-both behavior).

**Scope:**

1. Restore the 0.3s sleep between consecutive issue-mutation calls.
   Encapsulate the value as `_ISSUE_MUTATION_PAUSE_SECONDS = 0.3`.
2. Restore the proactive low-remaining throttle from `f2gh.py`'s
   `gh_request` (`main:82-84`): before each request, when
   `X-RateLimit-Remaining` is present and below 10, sleep 2 seconds
   before sending. This is the primary-rate-limit safeguard the
   refactor dropped alongside the 0.3s pacing.
3. Restore retry-both behavior in
   `GitHubClient._request_with_rate_limit_retry`: retry 429 and
   403-with-`X-RateLimit-Remaining: 0` up to 3 total attempts
   (`_MAX_ATTEMPTS = 3`) with `Retry-After` (or `X-RateLimit-Reset`)
   delay plus additive jitter `delay + random.uniform(0, _JITTER_SECONDS)`
   where `_JITTER_SECONDS = 1.0` (matching `f2gh.py`'s
   `retry_after + random.uniform(0, 2)` shape).
4. Re-lock the stage-02 test
   `test_403_with_zero_rate_limit_remaining_raises_rate_limit_error`
   → `test_403_with_zero_rate_limit_remaining_retries_then_raises`
   with user approval per `00-index.md` "no test weakening."
   Amendment reason: the current test enshrines the regression.
5. Revert the stage-02 spec amendment to `02-api-clients.md` §3.3
   (which codified 403-raises-immediately), with user approval.
6. Augment `test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error`
   to assert additive jitter, with user approval.
7. Addendum (discovered during Slice B GREEN, user-approved): a second
   stage-02 test pins the same regressive contract —
   `tests/test_api_clients.py::test_gh_request_403_with_remaining_zero_raises_rate_limit_error`
   (single scripted 403, asserts immediate `GitHubRateLimitError` with
   one transport call). Amended to the same retry-both contract as the
   re-locked sibling and renamed to
   `test_403_with_remaining_zero_retries_then_raises` (the legacy
   `gh_request` name no longer exists). Recorded here so this ledger
   remains the complete record of Slice B test amendments.

**Out of scope for Slice B:** the unbounded reset sleep (#13, backlog)
and pagination safeguard (#14, backlog). Those are genuine pre-existing
bugs and are tracked in §6.

**RED tests:**

- `test_orchestrator_pauses_between_issue_mutation_calls` (to be added)
- `test_github_client_proactive_sleep_when_remaining_low` (to be added;
  asserts a 2-second sleep precedes the request when
  `X-RateLimit-Remaining < 10` and the request still completes)
- `test_github_client_retry_includes_jitter` (to be added; asserts
  the sleep is in `[delay, delay + _JITTER_SECONDS]`)
- `test_403_with_zero_rate_limit_remaining_retries_then_raises` (rename
  of existing; user-approved amendment per §4.2)

**GREEN work:**

- Add `_ISSUE_MUTATION_PAUSE_SECONDS = 0.3` and inject the sleep
  through a `time.sleep` seam (or, per the locked constructor
  discipline, keep it as a module-level function and use
  `unittest.mock.patch` in tests).
- Restore the proactive throttle in
  `_request_with_rate_limit_retry`: read `X-RateLimit-Remaining`
  before dispatch; when present and below 10, sleep 2 s first
  (through the same injectable clock seam), then send the request.
- Modify `GitHubClient._request_with_rate_limit_retry` to retry
  403-with-zero-remaining alongside 429.
- Add a jitter helper that preserves the current additive shape:
  `_jittered_delay(base: float) -> float` returning
  `base + random.uniform(0, _JITTER_SECONDS)` where
  `_JITTER_SECONDS = 1.0` (matching `f2gh.py`'s
  `retry_after + random.uniform(0, 2)` and the current client's
  `_JITTER_SECONDS`). Do **not** introduce proportional jitter.

**Verification (Slice B):**

```bash
./scripts/run-tests.sh tests/test_github_client.py
./scripts/run-tests.sh tests/test_orchestration.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/github.py
ruff check forgejo_to_github/migration.py
mypy forgejo_to_github/github.py               # informational
```

**Stop gate (Slice B):** report the test rename, the test augmentation,
the jitter helper, and the verification outcomes. The test rename
is a "no test weakening" exception and must be explicitly approved.

---

### Slice C — Issue fidelity: labels, sorting, attribution, and the failure `kind`

**Resolves:** #1 (issue-body attribution), #4 (label dicts stringified),
#5 (ensure_label), #9 (issue sorting), #19 (issue_failed kind mismatch).

**Scope:**

1. Sort issues by `created_at` ascending before iterating.
2. Extract `label["name"]` for each label; do not stringify.
3. Wire `github.ensure_label(name, color)` for each label before
   `github.create_issue`, using `DEFAULT_LABEL_COLOR = "ededed"`
   when the source label has no `color` (per `04-orchestrator.md` §3.11).
4. Wrap the issue body with `format_issue_body(source, cb_index,
   author, date, body)` before passing to `github.create_issue`.
5. Fix the orchestrator's call to `reporter.issue_failed` to pass
   `(source_number, kind, message)` per the spec; remove the
   reporter's parameter-shuffling fallback.
6. The `kind` value is one of `"issue_create"`, `"comment"`,
   `"close_failed"`, `"label_create"` per `04-orchestrator.md` §3.9.

**Out of scope for Slice C:** comment-body attribution (Slice D),
counters (Slice E).

**RED tests (all `to be added`):**

- `test_orchestrator_migrates_issues_in_creation_date_order`
- `test_orchestrator_passes_label_names_not_dicts`
- `test_orchestrator_ensures_each_label_before_creating_issue`
- `test_orchestrator_uses_default_label_color_when_source_label_lacks_color`
- `test_orchestrator_wraps_issue_body_with_attribution_block`
- `test_reporter_issue_failed_receives_distinct_kind_per_failure_step`

**GREEN work:**

- Add the sort, label extraction, `ensure_label` call, and
  `format_issue_body` call to `MigrationOrchestrator._migrate_issue`
  (or equivalent).
- Update the `reporter.issue_failed` call site in the orchestrator
  to pass the third argument.
- Remove the reporter's `(source_number, message)` two-arg fallback.

**Verification (Slice C):**

```bash
./scripts/run-tests.sh tests/test_orchestration.py
./scripts/run-tests.sh tests/test_github_client.py
./scripts/run-tests.sh tests/test_reporting.py
./scripts/run-tests.sh tests/test_formatting.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/migration.py
ruff check forgejo_to_github/formatting.py
ruff check forgejo_to_github/reporting.py
mypy forgejo_to_github/migration.py             # informational
```

**Addendum (discovered during Slice C GREEN, user-approved):** two
test-side amendments were required to complete the slice, both recorded
here so this ledger remains the complete record:

1. `test_orchestrator_migrates_issues_in_creation_date_order` was
   unsatisfiable as written: its `_FakeApi.create_issue` derived the
   recorded issue number from call position, so it recorded API order
   regardless of actual creation order. Fixed by recording the title
   per `create_issue` call; the assertion (created_at-ascending) is
   contract-intact.
2. `test_github_wiring_creates_issue_and_comment_with_concrete_signatures`
   pinned the pre-Slice-C raw-body contract (`body == "Issue body"`)
   and positional POST assumptions that predate `ensure_label`.
   Amended to expect the attribution-wrapped body and to select/verify
   POSTs by URL with label traffic scripted (GET 404 → POST 201 per
   label, label traffic before the issue create).

**Stop gate (Slice C):** report the diff, the new test names, the
verification outcomes, and any spec deviation. Note that #20 is
resolved as a consequence (the formatting helpers are now live).

---

### Slice D — Comments fetch, format, counts

**Resolves:** #2 (comments not fetched), #3 (comment attribution),
#11 (skipped malformed comments counted).

**Scope:**

1. In the issue loop, call
   `codeberg.list_comments(issue_id=...)` per the `02-api-clients.md`
   contract. Iterate the returned list (not `issue.get("comments")`).
2. Wrap each comment body with `format_comment_body(author, date, body)`.
3. When a comment is filtered as malformed (e.g., non-integer id,
   empty body, missing author), increment `comments_attempted` so
   the count reflects what the operator sees in the source, and
   emit a one-line warning through the reporter.

**Out of scope for Slice D:** counters beyond comments (Slice E).

**Decisions (user-approved, 2026-09-06):**

1. Warning seam: new `Reporter.comment_skipped(source_number, reason)`
   method, routed to stderr. Reusing `issue_failed` was rejected — a
   skipped comment must not render as an issue failure.
2. Malformed filter: skip + warn when `type` is present and not
   `"Comment"`, OR body is missing/empty, OR author is missing. The
   scope item 3's "non-integer id" example is dropped — nothing
   consumes comment ids.
3. Fetch-failure mapping: `result.failures` step `"fetch_comments"`
   (granular, matches `main`'s step vocabulary); reporter kind
   `"comment"` (no extension of the locked kind vocabulary).
4. Scope addition: a fifth RED test pins the fetch-failure path.
5. Counter order: `comments_attempted` increments before the
   malformed filter (restates scope item 3 precisely).
6. Fetch is unconditional per issue, even when the issue payload's
   `comments` count is zero (matches the `main:f2gh.py` baseline).

**RED tests (all `to be added`):**

- `test_orchestrator_fetches_comments_via_codeberg_client`
- `test_orchestrator_wraps_comment_bodies_with_attribution`
- `test_orchestrator_counts_skipped_malformed_comments`
- `test_orchestrator_logs_warning_for_malformed_comment`
- `test_orchestrator_comment_fetch_failure_fails_issue` (scope
  addition, user-approved per decision 4)

**GREEN work:**

- Replace the comment-iteration in `MigrationOrchestrator.run()`
  with a call to `codeberg.list_comments(issue_id=...)`.
- Wrap each body with `format_comment_body`.
- Add the `comments_attempted` increment and reporter warning for
  the malformed-comment branch.

**Verification (Slice D):**

```bash
./scripts/run-tests.sh tests/test_orchestration.py
./scripts/run-tests.sh tests/test_codeberg_client.py
./scripts/run-tests.sh tests/test_formatting.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/migration.py
mypy forgejo_to_github/migration.py             # informational
```

**Stop gate (Slice D):** report the diff, the new test names, the
verification outcomes. Note that #20 is fully resolved (both
helpers are now live).

---

### Slice E — Resume truthfulness

**Resolves:** #10 (issues_attempted before resume), additional
finding §3.1 (`git_pushed` not persisted), additional finding §3.2
(resumed/skipped issues lack visible reporting).

**Scope:**

1. Move `result.issues_attempted += 1` to **after** the resume
   guard; only count issues the orchestrator actually begins work on.
2. After a successful push, set `_concrete_git_pushed = True` and
   persist via the existing concrete save path (`MigrationState` and
   `StateStore.save` already include `git_pushed`; the fix is purely
   orchestrator-side). On resume, if `git_pushed` is `True`, skip the
   **entire** Git phase (both clone and push, matching `main:673`)
   and record `git["clone"]="skipped"` and `git["push"]="skipped"`.
3. Add `reporter.issue_skipped(source_number)` and call it from the
   orchestrator when an issue is in `state.migrated` before S1.

**Out of scope for Slice E:** the prompter (Slice A), pacing (Slice B),
labels (Slice C), comments (Slice D), small fixes (Slice F).

**RED tests (all `to be added` unless noted):**

- `test_issues_attempted_excludes_resumed_issues`
- `test_state_records_git_pushed_after_successful_push_and_skips_on_resume`
- `test_state_does_not_record_git_pushed_when_push_fails`
- `test_reporter_emits_issue_skipped_message_for_resumed_issue`
- Augmentation of existing
  `tests/test_migration_reporting.py::test_successful_issues_are_checkpointed_and_resume_filters_them`
  to assert `result.issues_attempted` excludes resumed issues
  (user-approved amendment per `00-index.md` "no test weakening").

**GREEN work:**

- Move the increment in the orchestrator.
- Set `_concrete_git_pushed = True` after a successful push and persist
  via the existing concrete save path. `MigrationState.git_pushed`
  (`state.py:95`) and `StateStore.save` (`state.py:315`) already include
  the field; the on-disk format already has `git_pushed` with
  `False` default on load (`state.py:308`). No schema change is needed.
- Add the reporter method and the orchestrator call.

**Verification (Slice E):**

```bash
./scripts/run-tests.sh tests/test_orchestration.py
./scripts/run-tests.sh tests/test_state_store.py
./scripts/run-tests.sh tests/test_migration_reporting.py
./scripts/run-tests.sh tests/test_reporting.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/migration.py
ruff check forgejo_to_github/state.py
ruff check forgejo_to_github/reporting.py
mypy forgejo_to_github/migration.py             # informational
mypy forgejo_to_github/state.py                 # informational
```

**Stop gate (Slice E):** report the diff, the new test names, the
test augmentation, and the verification outcomes. No on-disk schema
change is needed — `git_pushed` already exists in `MigrationState`
(`state.py:95`) and `StateStore.save` (`state.py:315`); the fix is
purely orchestrator-side (setting and persisting the value after a
successful push).

---

### Slice F — Small fixes

**Resolves:** #17 (`rstrip(".git")`), #18 (Codehub typo).

**Scope:**

1. Replace `rstrip(".git")` with `removesuffix(".git")` in
   `GitMirror._tempdir_prefix`.
2. Change `"Codehub"` to `"Codeberg"` in the
   `CodebergClient.get_issue` 429 message.

**Out of scope for Slice F:** anything beyond these two fixes.

**RED tests (both `to be added`):**

- `test_git_mirror_tempdir_prefix_uses_removesuffix_not_rstrip`
- `test_codeberg_get_issue_rate_limit_message_says_codeberg`

**GREEN work:**

- One-line change in `forgejo_to_github/git.py`.
- One-word change in `forgejo_to_github/codeberg.py`.

**Verification (Slice F):**

```bash
./scripts/run-tests.sh tests/test_git_service.py
./scripts/run-tests.sh tests/test_codeberg_client.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/git.py
ruff check forgejo_to_github/codeberg.py
mypy forgejo_to_github/git.py                   # informational
mypy forgejo_to_github/codeberg.py              # informational
```

**Stop gate (Slice F):** report the two-line diff and verification.

---

### Slice G — End-to-end parity test

**Resolves:** the §4.6 locked parity test. No new production code
is expected from this slice; the slice exists to lock all the fixes
simultaneously in one test so future refactors cannot regress
multiple findings at once.

**Scope:**

1. Add the locked end-to-end parity test per §4.6.
2. Confirm that all of Slices A–F remain green when run together.

**Out of scope for Slice G:** any new production code beyond what
Slices A–F introduced. If the parity test reveals a missing seam or
helper, surface it for user approval before adding it.

**RED test:**

- `test_orchestrator_end_to_end_parity_with_real_payload_shapes`
  (to be added; per §4.6 and §1.4).

**GREEN work:** none expected. If GREEN requires production changes,
stop and surface the missing seam.

**Verification (Slice G):**

```bash
./scripts/run-tests.sh tests/test_orchestration.py
./scripts/run-tests.sh tests/test_migration_reporting.py
./scripts/run-tests.sh tests/test_cli.py
./scripts/run-tests.sh tests/test_state_store.py
./scripts/run-tests.sh tests/test_reporting.py
./scripts/run-tests.sh tests/test_formatting.py
./scripts/run-tests.sh tests/test_repository_description.py
./scripts/run-tests.sh tests/test_codeberg_client.py
./scripts/run-tests.sh tests/test_github_client.py
./scripts/run-tests.sh tests/test_git_service.py
./scripts/run-tests.sh                          # full suite
ruff check .
mypy f2gh.py
```

**Stop gate (Slice G):** report the parity test outcomes and the
full verification matrix from `00-index.md` §"High-level completion
criteria":

- The package has clear ownership of CLI, API, Git, state,
  orchestration, and reporting responsibilities.
- The `f2gh` entry point works without behavior regressions.
- Tests cover both successful and partial-failure paths, including
  clone/push errors and stateful resume.
- No test requires live credentials or network access.
- `./scripts/run-tests.sh`, `ruff check .`, and `mypy f2gh.py
  forgejo_to_github/` pass.

### 5.1 Final manual verification

After Slice G's GREEN stop report is approved, the user (per
AGENTS.md §3 stop gates) performs the final review and commit. The
implementing agent does **not** commit (per AGENTS.md §3 "No
commits"). The final manual verification steps the user may run:

1. **Live dry-run.** With `GITHUB_TOKEN` and `CODEBERG_TOKEN` set
   and a `--dry-run` invocation against a known small source repo,
   confirm the dry-run summary surfaces `would process N issues`
   (per `00-index.md` "Dry-run contract") and that `state.json` is
   unchanged by the dry run. (`--yes` is not needed here — dry-run
   never prompts.)
2. **Live non-destructive resume.** Pre-populate `state.json` with
   one checkpointed issue; run a non-`--dry-run`, non-`--skip-git`
   migration with `--yes`; confirm the checkpointed issue is
   skipped (no `create_issue` call), the GitHub repo is created if
   missing, labels are ensured, and the summary surfaces the skip.
3. **Live small migration.** Migrate a 2-issue, 3-comment source
   repo to a fresh target repo with `--yes`; confirm bodies and
   comments carry attribution, labels are created and applied,
   issues appear in `created_at` order, and the state file is
   committed.
4. **Live pacing observation.** During step 3, observe the
   inter-issue delay (e.g., via the GitHub audit log timestamps);
   confirm it is at least ~0.3s per issue-mutation call.
5. **Rate-limit retry.** If a sandbox supports it, force a 403-with-
   zero-remaining response from the GitHub client (e.g., by
   exhausting the primary rate limit on a personal token) and
   confirm the client retries up to 3 times with jittered delay
   before raising.

---

## 6. Backlog and Struck Items

This section exists so nothing disappears. Each item has a finding
reference, a reason it is not in the current remediation, and the
recommended next step.

### 6.1 Backlog (genuine pre-existing bugs, not regressions)

| Finding | Item | Reason | Recommended next step |
|---------|------|--------|------------------------|
| #13 | `GitHubClient._sleep_for_rate_limit` can sleep for hours | Genuine pre-existing bug. The fix (cap retry-after at e.g. 60s, plus jitter) is appropriate but is not a regression introduced by the refactor. | A future plan or a stage-02 amendment: cap `_sleep_for_rate_limit` at a maximum of 60s and fall back to `Retry-After` if `X-RateLimit-Reset` is farther in the future than the cap. Cross-reference `plans/03-keyboard-interrupt-handling.md` because an unbounded sleep also blocks Ctrl-C handling. |
| #14 | `CodebergClient._paginate` has no page limit safeguard | Genuine pre-existing bug. The fix (a `max_pages` parameter, default e.g. 1000) is appropriate but is not a regression. | A future plan or a stage-02 amendment: add `max_pages` to `CodebergClient._paginate` with a sensible default and a `ConfigurablePagination` seam for tests. Cross-reference `plans/04-retain-clone-cache.md` and `plans/05-local-clone-invocation.md` because cached/local clones will exercise pagination more heavily. |

### 6.2 Struck (intentional design or non-current bug)

| Finding | Item | Reason | Disposition |
|---------|------|--------|-------------|
| #15 | `StateStore.load` rejects unknown top-level keys | Intentional strict design per `01-state-store.md` §3.1. Forward compatibility is a future-schema concern, not a current bug. | Struck. If/when a schema version field is added (see #16), this strict policy will be revisited under user approval at that time. No test amendment; no production change. |
| #16 | `StateStore.save` does not preserve `version` key | Not a current bug. The old `f2gh.py` also wrote no version key. | Struck from current remediation. Tracked in §6.3 below as a future schema backlog item. |

### 6.3 Future schema backlog (separate from audit remediation)

- **State file `version` key.** Future work should add a `version`
  integer (start at `1`) to the on-disk format. When this is added,
  `StateStore.load` will use it to gate schema migrations, and the
  strict unknown-key policy (#15) will be revisited under user
  approval.
- **Token redaction hardening.** The orchestrator relies on
  `GitMirror.push_branches` / `push_tags` to redact tokens; the
  clients do not currently redact tokens from logged URLs or error
  messages. A future hardening pass should centralize redaction in
  a helper module and apply it to all error paths.

---

## 7. Traceability Template

Every audit finding maps to: a test, an implementation slice, and a
verification command. The template below is filled in for the
remediation as a whole; per-finding detail is in §2.

| Finding | Test | Implementation slice | Verification |
|---------|------|----------------------|--------------|
| #1 issue-body attribution | `test_orchestrator_wraps_issue_body_with_attribution_block` | Slice C | `./scripts/run-tests.sh tests/test_orchestration.py` |
| #2 comments not fetched | `test_orchestrator_fetches_comments_via_codeberg_client` | Slice D | `./scripts/run-tests.sh tests/test_orchestration.py` |
| #3 comment attribution | `test_orchestrator_wraps_comment_bodies_with_attribution` | Slice D | `./scripts/run-tests.sh tests/test_orchestration.py` |
| #4 label dicts stringified | `test_orchestrator_passes_label_names_not_dicts` | Slice C | `./scripts/run-tests.sh tests/test_orchestration.py` |
| #5 ensure_label missing | `test_orchestrator_ensures_each_label_before_creating_issue` | Slice C | `./scripts/run-tests.sh tests/test_orchestration.py tests/test_github_client.py` |
| #6 repo creation/description | `test_orchestrator_creates_target_when_missing`, `test_orchestrator_does_not_touch_description_when_target_exists`, `test_orchestrator_uses_fallback_description_on_source_fetch_failure` | Slice A | `./scripts/run-tests.sh tests/test_orchestration.py tests/test_repository_description.py` |
| #7 prompts / `--yes` | `test_orchestrator_prompts_before_creating_target`, `test_orchestrator_skips_prompts_when_yes_flag_set`, `test_orchestrator_prompts_when_target_has_existing_issues`, `test_orchestrator_aborts_when_prompter_returns_false`, plus RED design tests | Slice A | `./scripts/run-tests.sh tests/test_orchestration.py tests/test_cli.py` |
| #8 pacing | `test_orchestrator_pauses_between_issue_mutation_calls` | Slice B | `./scripts/run-tests.sh tests/test_orchestration.py` |
| #9 issue sorting | `test_orchestrator_migrates_issues_in_creation_date_order` | Slice C | `./scripts/run-tests.sh tests/test_orchestration.py` |
| #10 issues_attempted before resume | `test_issues_attempted_excludes_resumed_issues`, plus augmentation of `test_successful_issues_are_checkpointed_and_resume_filters_them` | Slice E | `./scripts/run-tests.sh tests/test_orchestration.py tests/test_migration_reporting.py` |
| #11 comments_attempted on malformed | `test_orchestrator_counts_skipped_malformed_comments`, `test_orchestrator_logs_warning_for_malformed_comment` | Slice D | `./scripts/run-tests.sh tests/test_orchestration.py` |
| #12 retry-both behavior | `test_403_with_zero_rate_limit_remaining_retries_then_raises` (renamed), `test_github_client_retry_includes_jitter` | Slice B | `./scripts/run-tests.sh tests/test_github_client.py` |
| #13 unbounded reset sleep | (none — backlog) | n/a | n/a |
| #14 unbounded pagination | (none — backlog) | n/a | n/a |
| #15 strict state keys | (none — struck) | n/a | n/a |
| #16 no state version | (none — struck) | n/a | n/a |
| #17 rstrip(".git") | `test_git_mirror_tempdir_prefix_uses_removesuffix_not_rstrip` | Slice F | `./scripts/run-tests.sh tests/test_git_service.py` |
| #18 Codehub typo | `test_codeberg_get_issue_rate_limit_message_says_codeberg` | Slice F | `./scripts/run-tests.sh tests/test_codeberg_client.py` |
| #19 issue_failed kind mismatch | `test_reporter_issue_failed_receives_distinct_kind_per_failure_step` | Slice C | `./scripts/run-tests.sh tests/test_orchestration.py tests/test_reporting.py` |
| #20 formatting dead code | (no separate test — verified by Slice C/D tests) | Slices C + D | `./scripts/run-tests.sh tests/test_formatting.py tests/test_orchestration.py` |
| Add. §3.1 git_pushed persistence | `test_state_records_git_pushed_after_successful_push_and_skips_on_resume`, `test_state_does_not_record_git_pushed_when_push_fails` | Slice E | `./scripts/run-tests.sh tests/test_state_store.py tests/test_orchestration.py` |
| Add. §3.2 resumed-issue reporting | `test_reporter_emits_issue_skipped_message_for_resumed_issue` | Slice E | `./scripts/run-tests.sh tests/test_reporting.py tests/test_orchestration.py` |
| Add. §3.3 phase order | `test_orchestrator_runs_preflight_before_git_phase` | Slice A | `./scripts/run-tests.sh tests/test_orchestration.py` |
| Slice G parity | `test_orchestrator_end_to_end_parity_with_real_payload_shapes` | Slice G | `./scripts/run-tests.sh` (full suite), `ruff check .`, `mypy f2gh.py forgejo_to_github/` |

### 7.1 Per-slice verification block (recap)

- **Slice A:** `test_orchestration.py`, `test_cli.py`,
  `test_repository_description.py`, `test_migration_reporting.py`,
  full suite; `ruff check forgejo_to_github/migration.py f2gh.py`;
  `mypy forgejo_to_github/migration.py` (informational).
- **Slice B:** `test_github_client.py`, `test_orchestration.py`,
  full suite; `ruff check forgejo_to_github/github.py
  forgejo_to_github/migration.py`;
  `mypy forgejo_to_github/github.py` (informational).
- **Slice C:** `test_orchestration.py`, `test_github_client.py`,
  `test_reporting.py`, `test_formatting.py`, full suite;
  `ruff check forgejo_to_github/migration.py
  forgejo_to_github/formatting.py forgejo_to_github/reporting.py`;
  `mypy forgejo_to_github/migration.py` (informational).
- **Slice D:** `test_orchestration.py`, `test_codeberg_client.py`,
  `test_formatting.py`, full suite;
  `ruff check forgejo_to_github/migration.py`;
  `mypy forgejo_to_github/migration.py` (informational).
- **Slice E:** `test_orchestration.py`, `test_state_store.py`,
  `test_migration_reporting.py`, `test_reporting.py`, full suite;
  `ruff check forgejo_to_github/migration.py
  forgejo_to_github/state.py forgejo_to_github/reporting.py`;
  `mypy forgejo_to_github/migration.py forgejo_to_github/state.py`
  (informational).
- **Slice F:** `test_git_service.py`, `test_codeberg_client.py`,
  full suite;
  `ruff check forgejo_to_github/git.py forgejo_to_github/codeberg.py`;
  `mypy forgejo_to_github/git.py forgejo_to_github/codeberg.py`
  (informational).
- **Slice G:** every per-slice verification block above; then
  full suite, `ruff check .`, `mypy f2gh.py`.

---

## 8. Step 0 Notes (Pre-Remediation State)

These are the bookkeeping notes the user must see before any RED work
in §5 begins.

### 8.1 AUDIT.md remains untouched

Per AGENTS.md §3 ("Plan approval") and the user's instruction in this
session, `AUDIT.md` is **not modified** by this remediation. The audit
report is the artifact under remediation; the remediation ledger is
this document. Future audits (if any) will produce their own
`AUDIT-<n>.md` and their own remediation ledger.

### 8.2 Pre-remediation commit state

Pre-approved dry-run implementation work (the `DryRunDiscovery` domain
value, the discovery-population slice, the Reporter informative-preview
slice, and the numeric comment-count fix) was committed before the audit
was written. As of this writing, the branch is `ahead 1` of
`origin/dev/003-package-refactor` (commit `912c99b`, the numeric
comment-count fix, is unpushed), and `AUDIT.md` plus this ledger are
untracked. The user should commit and push these before Slice A RED
begins.

For findings classified as intentional design (e.g., strict state
validation, #15), the production code does not change because the user
**explicitly approved** retaining the behavior — not because the spec
overrides the baseline (see §1.3).

For findings that are confirmed regressions, the implementation work
to fix them is **not yet committed**. The slices in §5 will produce
that work in RED → GREEN pairs, with user approval at each GREEN
stop gate, per AGENTS.md §3.

### 8.3 Issue #3 / plan archival waits for remediation completion

`plans/02-package-refactor-and-test-foundation/` is the active plan
for issue #3 (`https://github.com/zcutlip/forgejo-to-github/issues/3`).
Per AGENTS.md §4, the plan moves to `plans/archive/` when complete;
the issue is closed only after a plan completes (the close comment
names the completing commit(s) and verification status). Both the
plan archival and the issue close therefore wait until:

1. Slices A–G each report GREEN with user approval at the stop gate.
2. Slice G's final manual verification (per §5.1) is completed by
   the user (per AGENTS.md §3 "Final review and commit are performed
   by the user").
3. The final committing agent (per AGENTS.md §3 "No commits" and
   "Delegation tiers") commits the work.

Until those gates close, neither the plan directory nor the GitHub
issue is archived/closed. The implementing agent does **not**
archive, commit, push, or close the issue at any point in §5.

### 8.4 What this document is **not**

- Not an implementation. No code, tests, configuration, or other
  artifacts are produced by this document. The slices in §5 are
  spec, not code.
- Not a summary. Every AUDIT.md finding appears verbatim in §2.
- Not a commit, push, or PR. Per AGENTS.md §3, the user holds
  those gates.
- Not a modification of AUDIT.md. AUDIT.md is preserved untouched.
- Not a modification of the stage specs in `./refactor/`. Where
  this document re-states a binding decision from
  `00-index.md`, it cites the source; it does not amend it.

---

*End of audit remediation ledger.*
