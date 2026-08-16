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

---

## 3. Key Commands Reference

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
