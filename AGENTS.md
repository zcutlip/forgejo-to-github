# AGENTS.md

Guidelines, safety boundaries, and operational constraints for AI developer agents working on this repository.

---

## 1. Project Context & Purpose

This repository contains a lightweight Python script/utility for migrating project metadata—specifically **issues, comment threads, and labels**—from **Codeberg / Forgejo** instances to **GitHub**.

### Primary Technical Challenges Addressed
- **Authentication & Authorization**: Operating across two distinct REST API paradigms (Forgejo v1 API vs GitHub REST API v3).
- **Secondary Rate Limit Handling**: GitHub strictly rate-limits rapid POST/PATCH operations; operations must be throttled with jitter and backoff.
- **Identity Attribution**: API actions run under the executing token's identity. Original author attribution and timestamps must be formatted transparently into Markdown blocks.
- **Stateful Resumption**: Large or interrupted migrations must support atomic checkpointing via a simple JSON file to recover gracefully.

---

## 2. Agent Guidelines & Execution Constraints

When modifying or executing code in this codebase, AI agents **MUST** strictly adhere to the following rules:

### A. Environment & Security Boundaries
- **No Hardcoded Credentials**: Never write API tokens, passphrases, or personal tokens into source code, test suites, or configuration templates.
- **Environment Variable Priority**: Read credentials strictly from `GITHUB_TOKEN` and `CODEBERG_TOKEN` environment variables, or retrieve GitHub credentials via `gh auth token` subshell commands.
- **Sandboxed Operations**: All local file operations must be constrained within the project directory tree. Do not modify global system settings or external directories.
- **Destructive Action Safety**:
  - **NEVER** issue API requests that delete GitHub/Codeberg repositories, branch protections, or issue comment histories unless explicitly requested in test suites with mock endpoints.
  - Always default to `--dry-run` when running CLI commands.

### B. Code Style & Architecture Standards
- **Python Version**: Target Python 3.12+.
- **Typing**: Use strict type hints (`typing` module or native Python 3.12+ annotations) on all function definitions.
- **Structure**:
  - Keep the script modular and lightweight.
  - State persistence must rely on simple, human-readable file storage (`state.json`) using atomic writes (`os.replace`) to avoid file corruption during abrupt exits.
  - Maintain comment preservation and avoid stripping structural docstrings or inline operational comments when refactoring code.
  - **No cwd-relative paths for new files or directories.** New on-disk locations (state, cache, temp) must be cwd-independent: use `platformdirs` (user state/cache dirs) or an explicit CLI flag. Writing to the process cwd silently couples a run to the directory it was launched from — a known design mistake (`state.json`-in-cwd) that must not be repeated.
  - **Domain ownership**: When adding rules for a domain concept, identify who owns identity, validation, and lifecycle; don't let related rules accumulate across CLI and helper modules just because each function is stateless; introduce a class only to enforce invariants or coordinate collaborators, keeping pure parsing/formatting as functions.
- **Dependencies**: Keep external dependencies minimal (prefer standard library or light additions like `requests`). A single-purpose dependency with no transitive dependencies may be added with explicit user approval (e.g. `platformdirs`); otherwise prefer the standard library.

### C. Testing & Verification Rules
- This project uses a centralized virtualenv at `~/.virtualenvs/forgejo-to-github`. Do **not** create or activate a project-local `.venv`, and do **not** invoke `pytest` directly.
- All tests **must** be run via `./scripts/run-tests.sh [pytest args...]`. The script handles virtualenv selection and activation, then passes all arguments through to `pytest`.
- Before submitting PRs or finalizing changes, run:
  - `./scripts/run-tests.sh` for unit/integration test validation.
  - `ruff check .` and `mypy f2gh.py forgejo_to_github/` for static analysis and type safety.
- Test external API integrations using mocked responses (`responses` or `unittest.mock`) to avoid hitting live APIs during routine test suite runs.
- When a persisted schema keeps or gains a field, require a test asserting its producer-driven value, not just its store round-trip. A store that faithfully persists whatever it is handed still passes when the producer hands it the wrong value; only an end-to-end assertion through the producing code path catches that.

## 3. Enforced Workflow

- **Plan approval:** Code changes begin only after the user reviews/approves the plan. Developing a plan is not approval.
- **TDD staged gates:** Spec/plan changes, test changes, and
  implementation are separate approval stages, each ending in a stop:
  1. **Spec/plan amendment:** make the spec or plan changes, then stop
     for user approval. The user may commit manually or direct a
     commit.
  2. **RED:** once the spec/plan is approved and committed, make the
     test changes (new tests, amended tests) and establish RED, then
     stop for user approval. The user may commit manually or direct a
     commit.
  3. **GREEN:** only after the tests are approved and committed, do
     the implementation work, then stop again for user review/commit.
  4. **Contract-gap rewind:** if implementation reveals a test or
     contract gap, returning to RED to amend a test is fine — request
     approval before resuming GREEN.
- **Tests lock the contract:** the spec/plan defines the contract the
  tests will lock. Tests committed independently of implementation
  detect inadvertent test/contract drift during implementation. Never
  change tests to make an implementation pass.
- **"Locked" is scoped to the effort:** a locked test/contract is a
  guardrail that stops the agent drifting *during* the current
  test/fix/implement/refactor cycle; referring to it during that work is
  expected. It does not transcend that effort — any prior decision can be
  changed by opening a new issue, so never present a past decision as
  immutable or as something to argue around. A **new issue gets its own
  fresh RED/GREEN cycle**: there is no "RED reopen" for new work;
  "reopen RED" applies only when amending an already-committed test within
  an in-flight issue.
- **RED honesty:** A new test must fail for the contract reason. If it passes immediately, keep it only as a disclosed guard stating why it can't fail yet — never silently keep a vacuous pass. Importing a not-yet-existing symbol, failing collection for the whole file, is an accepted RED shape here (not something to work around with lazy imports).
- **Stop gates:** User-held review checkpoints. After each substantive stage, stop for user review/approval. Final review is performed by the user.
- **No autonomous commits:** Agents may commit only when the user explicitly prompts it in the current conversation. Never commit, push, or stage-then-commit unprompted — automated checks and delegate reports do not constitute user approval. Never prompt or remind the user that a commit could or should happen; commit opportunities are the user's to notice.
- **Delegation tiers:** @lint and @commit are specialists and receive outcomes only — @commit is never without being explicitly directed by the user. @coder and @explore are generalists and may receive precise specifications. When parallel delegates disagree, or a delegate's claim gates what you report, re-run it yourself before reporting rather than trusting either report.


## 4. Planning and Issue Workflow

- Keep active implementation plans in `plans/`, numbered in dependency order.
- **Audit files are findings-only:** `plans/*-audit.md` files document findings and recommendations; they never change the spec. Adopting a finding is a separate spec-amendment step with its own approval.
- Identify each active plan's primary GitHub issue near the top of the plan; keep related issues under `References`.
- Move completed plans to `plans/archive/` rather than deleting them.
- Comment on a plan's issue when it adds value for an outside reader —
  e.g. the resulting state of the work, or the completing commit(s) and
  verification status when the issue is closed manually. When a merge
  commit will auto-close the issue, do **not** post a completion comment;
  post state only, or nothing.
- **External voice for GitHub issues:** Write issues in problem/solution/verification terms for an outside reader. Never cite slice letters, RED/GREEN phases, stop gates, spec files, ledger sections, or memory IDs — those are internal workflow artifacts.
- **Spec prose and locked tests move together:** When amending a locked test's contract (e.g., raising a threshold), amend the spec prose documenting the rule in the same change. A test-only amendment leaves the spec contradicting the test.
- **Deliberate contract changes name their stale tests:** When a plan changes a locked contract (e.g., a required flag becomes optional), the spec's test list must name the existing tests that lock the old shape, so RED reopens them explicitly instead of GREEN discovering them as failures.
- **Pin every new exit path's code in the spec:** When the spec introduces user-facing exit paths (usage errors, declines, aborts), each exit code is part of the contract — an unpinned code gets invented at RED and may contradict an existing path.
- Treat `plans/archive/02-package-refactor-and-test-foundation/` (the staged
  refactor specification under `refactor/00-index.md`) as the test and
  architecture foundation for later plans; do not implement later
  cross-cutting features in the monolithic script first.
- Keep clone failures terminal; Git push failures may continue to issue migration; `--skip-git` is the explicit issue-only path.
- **Branch workflow:** Do issue work on a dedicated branch named
  `dev/<issue-number>-<short-slug>` (e.g. `dev/8-rate-limit-sleep-cap`).
  Multi-issue plans use one branch named after the plan instead
  (matches the historical `dev/003-package-refactor`). Branch from a
  current `main`; one branch per issue or plan, never batch unrelated
  issues onto one branch.
- Commits happen on the branch, only when the user explicitly prompts
  them (§3 No autonomous commits); merge, push, pull, and fetch remain
  manual per §4.
- **Merge, push, pull, and fetch are manual:** agents never merge,
  push, pull, or fetch. Agents may create and switch *local* branches
  only; everything touching other refs or remotes is performed by the
  user.
- Merge to `main` after the stop-gate review (done manually by the
  user); delete the branch (local and origin) afterward —
  `submodules/repo-mgmt-scripts/` `deletebranch` does both in one
  pass.
- Single-file trivial fixes may go directly to `main` at user
  discretion; when in doubt, use a branch.

---

## 5. Key Commands Reference

```bash
# Project virtualenv (managed centrally, do not create a project-local .venv)
# Path: ~/.virtualenvs/forgejo-to-github
# Run tests via the wrapper — it activates the venv and forwards args to pytest:
./scripts/run-tests.sh

# Editable install (one-time, inside the project virtualenv or via pipx)
pip install -e .   # editable install → `f2gh` command (or `pipx install .`)

# Run migration in dry-run mode
f2gh --source owner/repo --target owner/repo --dry-run
# (no-install fallback: ./f2gh.py --source owner/repo --target owner/repo --dry-run)

# Run linter
ruff check .
```

### Releases

Fully manual — there is no PyPI publish step; `README.md` installs via
`pipx` from the git URL.

1. Bump `__version__` in `forgejo_to_github/__about__.py`. This is the
   **single source of truth**: `pyproject.toml` reads it dynamically
   (`[tool.setuptools.dynamic]`), so never bump the version there.
2. Commit, tag `vX.Y.Z`, push.
3. Create the GitHub release.

User-visible behavior changes are a minor bump; test-only or doc-only
changes are not released on their own.
