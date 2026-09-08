# Audit Report: forgejo-to-github Production Code

Audit performed on the refactored `forgejo_to_github` package and `f2gh.py`
CLI. Tests were not audited (all passing as of this writing). Findings are
limited to production code.

---

## Critical Bugs (Breaks Core Functionality)

### 1. Issue bodies are not formatted with migration attribution
- **Location:** `forgejo_to_github/migration.py:433-434`
- **Problem:** The orchestrator passes `issue.get("body", "")` directly to
  `github.create_issue()`. It never calls `format_issue_body()`. The old
  `f2gh.py` wrapped every issue body with `format_issue_body(source, cb_index,
  author, date, body)` to add the "Migrated from Codeberg" attribution block.
  That call is gone.
- **Impact:** Migrated issues lose the "Migrated from Codeberg" attribution,
  original author, date, and link back to the original Codeberg issue. This is
  a major regression in migration fidelity.

### 2. Comments are not fetched from Codeberg
- **Location:** `forgejo_to_github/migration.py:458`
- **Problem:** The orchestrator iterates `issue.get("comments") or []`. The
  real Codeberg API returns `"comments": 3` (an integer count), not a list. The
  orchestrator never calls `codeberg.list_comments(issue_id)` to fetch actual
  comment bodies. The old code called `fetch_codeberg_comments(source,
  cb_index)` for every issue.
- **Impact:** Zero comments are migrated. The
  `comments_attempted`/`comments_succeeded` counters stay at 0. This is a total
  loss of comment migration.

### 3. Comment bodies are not formatted with attribution
- **Location:** `forgejo_to_github/migration.py:467`
- **Problem:** Even if comments were fetched, the orchestrator passes
  `comment.get("body", "")` directly to `github.create_comment()`. It never
  calls `format_comment_body(author, date, body)`. The old code wrapped every
  comment with author and date attribution.
- **Impact:** Comments lose author and date attribution.

### 4. Labels are passed as raw dicts, not names
- **Location:** `forgejo_to_github/migration.py:435-439`
- **Problem:** `labels_raw = issue.get("labels")` yields a list of label dicts
  from the Codeberg API (e.g., `[{"id": 1, "name": "bug", "color":
  "f29513"}]`). The orchestrator does `[str(lbl) for lbl in list(labels_raw)]`,
  which stringifies the dicts into `"{'id': 1, 'name': 'bug', ...}"`. The old
  code extracted `label["name"]` for each label.
- **Impact:** GitHub receives garbage label strings like `"{'id': 1, 'name':
  'bug'}"` instead of `"bug"`. Issue creation will likely fail with a 422
  validation error, or create nonsense labels.

### 5. No label creation/ensuring before issue creation
- **Location:** `forgejo_to_github/migration.py` (missing)
- **Problem:** The old code called `ensure_label` for each label before
  creating the issue. The new orchestrator never calls `github.ensure_label()`.
  If a label doesn't exist on the target repo, GitHub issue creation with that
  label will fail with a 422 validation error.
- **Impact:** Issues with labels will fail to create unless the labels already
  exist on the target repo.

### 6. No repository creation or description update
- **Location:** `forgejo_to_github/migration.py` (missing)
- **Problem:** The old code checked if the target repo existed, created it if
  missing (with description from Codeberg or explicit override), and updated
  the description if explicitly provided. The new orchestrator never calls
  `github.create_repository()` or `github.update_repository_description()`. The
  spec (`04-orchestrator.md` §3.2 step 2) explicitly requires this.
- **Impact:** If the target repo doesn't exist, the migration crashes when
  trying to create issues on a non-existent repo. If the user provided
  `--description`, it's ignored.

### 7. No interactive confirmation prompts
- **Location:** `f2gh.py` (missing)
- **Problem:** The old code prompted for confirmation before creating a repo or
  when migrating into a repo with existing issues. The `--yes` flag skipped
  prompts. The new CLI has no prompts at all.
- **Impact:** Users can accidentally migrate into the wrong repo or create
  repos without confirmation. The `--yes` flag is now a no-op.

### 8. No rate limiting between API calls
- **Location:** `forgejo_to_github/migration.py` (missing)
- **Problem:** The old code had `time.sleep(0.3)` between issue creation,
  comment creation, and issue close. The new orchestrator fires API calls as
  fast as possible.
- **Impact:** Rapid-fire POST/PATCH requests trigger GitHub secondary rate
  limits, causing 429/403 errors and failed migrations.

### 9. No issue sorting by creation date
- **Location:** `forgejo_to_github/migration.py` (missing)
- **Problem:** The old code sorted issues by `created_at` before migrating. The
  new orchestrator processes issues in API pagination order (which is typically
  reverse chronological or undefined).
- **Impact:** Issues are migrated in arbitrary order, making it harder to
  correlate source and target issue numbers.

---

## High Bugs (Data Loss / Incorrect Behavior)

### 10. `issues_attempted` incremented before resume check
- **Location:** `forgejo_to_github/migration.py:419-426`
- **Problem:** `result.issues_attempted += 1` happens before
  `self._already_migrated(source_number)`. Already-migrated issues are counted
  as "attempted" even though no work is done.
- **Impact:** The final summary reports inflated `issues_attempted` counts on
  resumed runs.

### 11. `comments_attempted` not incremented for skipped malformed comments
- **Location:** `forgejo_to_github/migration.py:460-464`
- **Problem:** If a comment has a malformed index (non-integer), the
  orchestrator `continue`s without incrementing `comments_attempted`. The
  comment is silently skipped.
- **Impact:** Malformed comments are silently dropped with no record in the
  result.

---

## Medium Bugs (Edge Cases / Robustness)

### 12. `GitHubClient._request_with_rate_limit_retry` only retries 429, not 403 with `X-RateLimit-Remaining: 0`
- **Location:** `forgejo_to_github/github.py:408-431`
- **Problem:** The docstring says "Only HTTP 429 responses are retried. A 403
  with `X-RateLimit-Remaining: 0` is translated to `GitHubRateLimitError`
  immediately, without an internal retry." But the spec (`02-api-clients.md`
  §3.3) says 403 with `X-RateLimit-Remaining: 0` should also be retried up to 3
  times. The code only retries 429.
- **Impact:** Primary rate limit responses (403 with `X-RateLimit-Remaining:
  0`) are not retried, causing immediate failure instead of backoff-and-retry.

### 13. `GitHubClient._sleep_for_rate_limit` can sleep for a very long time
- **Location:** `forgejo_to_github/github.py:438-455`
- **Problem:** If `X-RateLimit-Reset` is far in the future, `retry_after =
  max(reset_epoch - int(time.time()), 1)` can be hours. The client will sleep
  for hours without any cap.
- **Impact:** Migration appears to hang for hours on primary rate limit.

### 14. `CodebergClient._paginate` has no page limit safeguard
- **Location:** `forgejo_to_github/codeberg.py:337-406`
- **Problem:** The pagination loop runs until an empty page is returned. If the
  API has a bug and never returns an empty page, the loop runs forever.
- **Impact:** Infinite loop on API misbehavior.

### 15. `StateStore.load` rejects unknown top-level keys
- **Location:** `forgejo_to_github/state.py:245-251`
- **Problem:** Any unknown key in `state.json` causes `StateLoadError`. If a
  future version adds a key, old versions crash.
- **Impact:** Forward compatibility is broken. A state file written by a newer
  version crashes older versions.

### 16. `StateStore.save` does not preserve `version` key
- **Location:** `forgejo_to_github/state.py:337-344`
- **Problem:** `save()` writes a payload without a `version` key. On reload,
  the missing version is accepted (legacy compatibility), but the file is
  effectively downgraded to legacy format.
- **Impact:** The state file never carries a version marker, making future
  schema migrations harder.

### 17. `GitMirror._tempdir_prefix` uses `rstrip(".git")` which strips character set, not suffix
- **Location:** `forgejo_to_github/git.py:676-687`
- **Problem:** `rstrip(".git")` strips any trailing characters that appear in
  the set `{'.', 'g', 'i', 't'}`, not the literal suffix `".git"`. For a repo
  named `tagging.git`, `rstrip(".git")` would strip trailing `g`, `i`, `t`, `.`
  characters, potentially mangling the name. For example,
  `"https://github.com/owner/tagging.git"` becomes
  `"https://github.com/owner/ta"` because the the trailing `g`, `i`, `t` chars
  are stripped, leaving `"taggng"` → then more chars... the result is
  unpredictable and depends on the repo name. The correct approach is to check
  for and strip the `".git"` suffix with `removesuffix(".git")` or equivalent.
- **Impact:** Tempdir prefixes (and thus tempdir names) are mangled for repos
  whose names end in characters from the set `{'.', 'g', 'i', 't'}`. This is a
  latent bug for repos with names like `tag`, `git`, `split`, etc.

---

## Low (Style / Maintainability)

### 18. `CodebergClient.get_issue` has a typo in error message
- **Location:** `forgejo_to_github/codeberg.py:249`
- **Problem:** The error message says `"Codehub rate limit exceeded"` instead
  of `"Codeberg rate limit exceeded"`.
- **Impact:** Cosmetic, but visible in user-facing error output.

### 19. `Reporter.issue_failed` signature mismatch with orchestrator
- **Location:** `forgejo_to_github/reporting.py:94-110`
- **Problem:** The spec defines `issue_failed(source_number, kind, message)`
  but the orchestrator calls it as `issue_failed(source_number, message)` (two
  args). The reporter handles this by treating `kind` as the message when
  `message` is `None`, and defaulting `kind` to `"issue_create"`. This works
  but is fragile — a failure with a non-create `kind` will be mislabeled as
  `issue_create`.
- **Impact:** Comment and close failures are mislabeled as `issue_create` in
  the failure output.

### 20. `format_issue_body` and `format_comment_body` are dead code
- **Location:** `forgejo_to_github/formatting.py`
- **Problem:** These functions are imported nowhere in production code. The
  orchestrator does its own (broken) body construction inline. They are only
  exercised by characterization and formatting tests.
- **Impact:** The migration attribution logic exists but is never wired in. The
  tests pass but the feature is dormant.
