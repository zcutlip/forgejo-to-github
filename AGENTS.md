Guidelines, safety boundaries, and operational constraints for AI developer agents working on this repository.

---

## 1. Project Context & Purpose

Migrates issue metadata — issues, comment threads, labels — from Codeberg/Forgejo to GitHub. The constraints below exist because:

- **Two API paradigms.** Forgejo v1 vs GitHub REST v3 differ in auth, pagination, and error shapes.
- **GitHub's primary rate limit returns `403`** with `X-RateLimit-Remaining: 0`; `429` is mostly *secondary* limits. Retry both, or the common signal ends the run.
- **API actions run under the executing token's identity.** Original author and timestamp are rendered into attribution text, not set on the created object.
- **State must survive abrupt exit.** Checkpoint writes are atomic (`os.replace` + fsync).

---

## 2. Agent Guidelines & Execution Constraints

### A. Environment & Security Boundaries
- **No hardcoded credentials** in source, tests, or config templates.
- **Credentials come from** `GITHUB_TOKEN` / `CODEBERG_TOKEN`, or `gh auth token` in a subshell.
- **File operations stay inside the project tree.** No global system settings or external directories.
- **NEVER** issue API requests that delete repos, branch protections, or comment histories unless explicitly requested in a test with mock endpoints.
- **Default to `--dry-run`** for CLI commands.

### B. Code Style & Architecture
- Python 3.12+, with strict type hints on all function definitions.
- Keep the script modular and lightweight.
- **No cwd-relative paths** for new files or directories — use `platformdirs` (user state/cache dirs) or an explicit CLI flag. `state.json`-in-cwd is a known design mistake; never repeat it.
- **State persistence** uses simple, human-readable storage with atomic writes (`os.replace`).
- **Preserve comments and docstrings** when refactoring; don't strip structural or operational comments.
- **Domain ownership:** when adding rules for a domain concept, decide who owns identity, validation, and lifecycle. Don't let them accumulate across CLI and helper modules just because each function is stateless. Introduce a class only to enforce invariants or coordinate collaborators; pure parsing and formatting stay functions.
- **Dependencies stay minimal** (stdlib, or light additions like `requests`). A single-purpose dependency with no transitive deps needs explicit user approval (e.g. `platformdirs`).

### C. Testing & Verification
- Centralized venv at `~/.virtualenvs/forgejo-to-github`. Never create or activate a project-local `.venv`; never invoke `pytest` directly.
- Run tests only via `./scripts/run-tests.sh [pytest args...]` — it activates the venv and forwards arguments to `pytest`.
- Before finalizing: `./scripts/run-tests.sh` and `mypy f2gh.py forgejo_to_github/`.
- Lint and format are the lint-format skill's remit and `@lint`'s to perform. Never invoke `ruff` directly, and never tell `@lint` how to do its job — give scope only.
- **A clean lint report is not evidence nothing changed.** The skill's `fix` runs `ruff check --fix` then formats but discards the check output, so it always reports `issues: []` and "N files unchanged" even when it edited files. Diff the files, or check mtimes, after every delegated run.
- Mock external APIs (`responses` or `unittest.mock`); never hit live APIs in the suite.
- **A persisted schema field needs a test asserting its producer-driven value**, not just a store round-trip — a store faithfully persists whatever it is handed, including the wrong value.
- **A strengthened assertion must be shown to fail.** When tightening an existing test, verify the new assertion breaks against the regression it guards; a green run proves nothing on its own.
- **Cross-seam ordering needs one sequence.** An index into one collaborator's log cannot be compared against an index into another's — the verdict then depends on each list's starting offset rather than on the ordering. Use a single shared timeline.

---

## 3. Enforced Workflow

- **Plan approval:** code changes begin only after the user reviews and approves the plan. Developing a plan is not approval.
- **TDD staged gates** — spec/plan, tests, and implementation are separate approval stages, each ending in a stop:
  1. **Spec/plan amendment**, then stop for approval. The user may commit or direct a commit.
  2. **RED** — with the plan approved and committed, make the test changes and establish RED, then stop for approval.
  3. **GREEN** — only after the tests are approved and committed, implement, then stop again.
  4. **Contract-gap rewind** — if implementation reveals a test or contract gap, returning to RED is fine; request approval before resuming GREEN.
- **Test-remediation-only work has no GREEN stage.** RED is where the amendments are applied; exiting RED is the user approving and committing the updated tests. GREEN is a no-op unless remediation surfaces a real implementation issue, and then only with its own approval.
- **Tests lock the contract:** the plan defines the contract the tests will lock, and tests committed independently of implementation catch drift. Never change tests to make an implementation pass.
- **"Locked" is scoped to the effort** — a guardrail that stops drift during the current cycle. Any prior decision can be changed by opening a new issue, so never present one as immutable or as something to argue around. A new issue gets its own fresh RED/GREEN cycle; "reopen RED" applies only to amending an already-committed test within an in-flight issue.
- **RED honesty:** a new test must fail for the contract reason. If it passes immediately, keep it only as a disclosed guard stating why it can't fail yet. Importing a not-yet-existing symbol, or failing collection for the whole file, is an accepted RED shape — not something to work around with lazy imports.
- **Stop gates:** user-held review checkpoints after each substantive stage. Final review is the user's.
- **No autonomous commits:** commit only when the user explicitly prompts it in the current conversation — never unprompted, never stage-then-commit. Automated checks and delegate reports are not approval. Never prompt that a commit could happen; commit opportunities are the user's to notice.
- **Delegation tiers:** `@lint` and `@commit` are specialists receiving outcomes only; `@commit` never without explicit direction. `@coder` and `@explore` may receive precise specifications. When a delegate's claim gates what you report, re-run it yourself rather than trusting either report.

---

## 4. Planning and Issue Workflow

- Active plans live in `plans/`, numbered in dependency order. Move completed plans to `plans/archive/` rather than deleting them.
- Name the plan's primary GitHub issue near the top; keep related issues under `References`.
- **Audit files are findings-only** — `plans/*-audit.md` records findings and recommendations and never changes the spec. Adopting one is a separate spec-amendment step with its own approval.
- **Spec prose and locked tests move together.** Amending a locked contract means amending the prose documenting that rule in the same change.
- **Deliberate contract changes name their stale tests**, so RED reopens them explicitly instead of GREEN discovering them as failures.
- **Pin every new exit path's code in the spec** — usage errors, declines, and aborts are all contract; an unpinned code gets invented at RED and may contradict an existing path.
- **External voice for issues:** write in problem/solution/verification terms for an outside reader. Never cite slice letters, RED/GREEN phases, stop gates, spec files, or memory IDs.
- Comment on a plan's issue when it adds value for an outside reader — the resulting state, the completing commits, or verification status when the issue is closed manually. When a merge commit will auto-close it, post state only, or nothing.
- Keep clone failures terminal; Git push failures may continue to issue migration; `--skip-git` is the explicit issue-only path.
- Treat `plans/archive/02-package-refactor-and-test-foundation/` (`refactor/00-index.md`) as the test and architecture foundation for later plans; don't implement later cross-cutting features in the monolithic script first.

### Branches

- Issue work goes on `dev/<issue-number>-<short-slug>`, created with `scripts/issue-branch create <type> <issue-number> <short-slug>`. Multi-issue plans use one branch named after the plan. Branch from a current `main`; one branch per issue or plan, never batch unrelated issues onto one branch.
- Agents may create and switch *local* branches only. Merge, push, pull, and fetch are the user's — in practice by directing `scripts/issue-branch finish`, which performs the merge itself (see §5).
- Merge to `main` after the stop-gate review; delete the branch (local and origin) afterward — `submodules/repo-mgmt-scripts/` `deletebranch` does both in one pass.
- Single-file trivial fixes may go directly to `main` at user discretion; when in doubt, use a branch.

---

## 5. Key Commands Reference

| Task | Command |
|---|---|
| Test suite | `./scripts/run-tests.sh` |
| One test file | `./scripts/run-tests.sh tests/test_foo.py` |
| Types | `mypy f2gh.py forgejo_to_github/` |
| Lint / format | delegated to `@lint` — never hand-run `ruff` |
| Editable install | `pip install -e .` (or `pipx install .`) |
| Dry-run migration | `f2gh --source owner/repo --target owner/repo --dry-run` |
| No-install fallback | `./f2gh.py --source owner/repo --target owner/repo --dry-run` |

### issue-branch lifecycle

```bash
scripts/issue-branch create <type> <issue-number> <short-slug>  # feature|fix|docs|refactor|test|chore
scripts/issue-branch resume <type> <issue-number> <short-slug>
scripts/issue-branch status
scripts/issue-branch bump-version --major|--minor|--patch
scripts/issue-branch finish
scripts/issue-branch release
```

- `status` is read-only and reports the issue, current version, release version, and changelog readiness — run it before choosing a release command.
- **`finish` is the normal end-of-branch step.** It runs the tests, rebases onto `main`, promotes `[Unreleased]`, commits the version, **merges `--no-ff` into `main`**, and tags. There is no separate manual merge.
- **`finish` takes no bump level.** `create` already wrote a dev version encoding the intended bump — `1.4.0.dev0` releases as `1.4.0`.
- **`release` requires you to be on `main`** and never mutates the version string. It reads the version, promotes `[Unreleased]`, commits, and tags. It refuses a dev/local-suffixed version (use `finish` or `bump-version`) and refuses an already-tagged version.
- **Version mutation belongs to four subcommands:** `create` and `finish` are the normal path (start bumps by branch type plus a dev suffix; `finish` strips to the reserved core). `resume` and `bump-version` are valves — they only restore or re-level a version the normal path would have produced, never invent one.
- **Work committed straight to `main` has no subcommand that can number it.** Edit `__about__.py` to the new version yourself, then run `release`. The already-tagged guard makes a forgotten bump fail loudly instead of producing a duplicate changelog heading.
- The helper automates commits: never run it on your own initiative, and never pass `--skip-tests` or `--skip-changelog` (those exist for direct user invocation). It never pushes and never creates a GitHub release.

### Releases

Manual once the helper has committed and tagged — there is no PyPI publish step; `README.md` installs via `pipx` from the git URL.

1. Push the branch and tags.
2. Create the GitHub release.

Version truth lives in `forgejo_to_github/__about__.py`; `pyproject.toml` reads it dynamically, so never bump the version there. User-visible behavior changes are a minor bump; test-only or doc-only changes are not released on their own.
