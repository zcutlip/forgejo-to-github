# Watchdog notes

Especially watch for:

- **Credential leaks**: any API token, passphrase, or secret written into
  source, tests, or config templates. Credentials belong in `GITHUB_TOKEN`,
  `CODEBERG_TOKEN`, `gh auth token` subshells, or env-var indirection — never
  hardcoded.
- **Destructive API calls**: any request that deletes or mutates GitHub/Codeberg
  repositories, branch protections, or issue/comment history outside explicit
  mocked test endpoints. CLI invocations that omit `--dry-run` when the default
  should be dry-run are suspect.
- **Environment violations**: creating a project-local `.venv`, invoking
  `pytest` directly, or bypassing `./scripts/run-tests.sh`. The virtualenv is
  centralized at `~/.virtualenvs/forgejo-to-github`.
- **Locked-test tampering**: edits to existing tests made to make an
  implementation pass instead of fixing the implementation. Tests are the
  contract; amendments need explicit user approval.
- **State-file corruption risks**: changes to checkpoint persistence that
  replace atomic writes (`os.replace`) with in-place writes to `state.json`.
- **Scope creep in the monolith**: new cross-cutting features implemented in
  `f2gh.py` when the staged refactor foundation
  (`plans/archive/02-package-refactor-and-test-foundation/`) requires them in
  the package structure.
- **Clone/push semantics**: treating a git clone failure as recoverable (it is
  terminal), or aborting issue migration on a git push failure (it should
  continue; `--skip-git` is the explicit issue-only path).
