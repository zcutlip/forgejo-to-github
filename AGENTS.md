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
- **Python Version**: Target Python 3.10+.
- **Typing**: Use strict type hints (`typing` module or native Python 3.10+ annotations) on all function definitions.
- **Structure**:
  - Keep the script modular and lightweight.
  - State persistence must rely on simple, human-readable file storage (`state.json`) using atomic writes (`os.replace`) to avoid file corruption during abrupt exits.
  - Maintain comment preservation and avoid stripping structural docstrings or inline operational comments when refactoring code.
- **Dependencies**: Keep external dependencies minimal (prefer standard library or light additions like `requests`).

### C. Testing & Verification Rules
- Before submitting PRs or finalizing changes, run:
  - `pytest` for unit/integration test validation.
  - `ruff check .` and `mypy f2gh.py` for static analysis and type safety.
- Test external API integrations using mocked responses (`responses` or `unittest.mock`) to avoid hitting live APIs during routine test suite runs.

## 3. Enforced Workflow

- **Plan approval:** Code changes begin only after the user reviews/approves the plan. Developing a plan is not approval.
- **TDD order:** Write tests first and establish RED, then implement to GREEN. Tests are the locked contract — never change tests to make an implementation pass.
- **RED contract gaps:** If RED exposes a legitimate contract gap, stop and surface it. With user approval, amend the test, then stop again for user approval of the amended test before resuming GREEN.
- **Stop gates:** User-held review checkpoints. After each substantive stage, stop for user review/approval. Final review and commit are performed by the user.
- **No commits:** Unless you are the @commit agent, never commit, push, or stage-then-commit. Automated checks and delegate reports do not constitute user approval.
- **Delegation tiers:** @lint and @commit are specialists and receive outcomes only — @commit is never without being explicitly directed by the user. @coder and @explore are generalists and may receive precise specifications.


## 4. Planning and Issue Workflow

- Keep active implementation plans in `plans/`, numbered in dependency order.
- Identify each active plan's primary GitHub issue near the top of the plan; keep related issues under `References`.
- Move completed plans to `plans/archive/` rather than deleting them.
- Before closing a plan's issue, comment with the completing commit(s) and verification status.
- Treat `plans/02-package-refactor-and-test-foundation.md` as the test and architecture foundation for later plans; do not implement later cross-cutting features in the monolithic script first.
- Keep clone failures terminal; Git push failures may continue to issue migration; `--skip-git` is the explicit issue-only path.

---

## 5. Key Commands Reference

```bash
# Environment setup
python -m venv .venv
source .venv/bin/activate
pip install -e .   # editable install → `f2gh` command (or `pipx install .`)

# Run migration in dry-run mode
f2gh --source owner/repo --target owner/repo --dry-run
# (no-install fallback: ./f2gh.py --source owner/repo --target owner/repo --dry-run)

# Run linter & tests
ruff check .
pytest
```
