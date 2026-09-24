# Shared issue-branch lifecycle workflow

**GitHub issue:** TBD — file before RED.
**Branch:** TBD — dedicated workflow branch after the issue is filed.
**Status:** spec — no tests or implementation yet.

## Context

`forgejo-to-github` has an issue/branch/changelog/release policy, but no automation:

- work happens on `dev/<issue-number>-<short-slug>`;
- `CHANGELOG.md` maintains `[Unreleased]`;
- releases are manual GitHub releases, with no PyPI publication;
- agents must not initiate commits or remote operations without explicit user direction.

`repo-mgmt-scripts` already supplies reusable branch, tag, and commit helpers, but has no issue-branch lifecycle command. `rag-mcp` has one, but it is hardcoded to that project and cannot be imported directly.

This plan adds a project-configurable lifecycle command to `repo-mgmt-scripts`, then integrates it with `forgejo-to-github`.

## Locked contract

### 1. Command surface

Add this executable command to the shared submodule:

```text
src/issue-branch
```

It supports:

```text
create <type> <issue-number> <short-slug>
status
bump-version --major|--minor|--patch
finish
release
```

No `.sh` extension, matching the shared repository’s naming policy.

### 2. Branch naming

The shared command supports a project-configurable template. This project uses:

```text
dev/{issue}-{slug}
```

Therefore:

```text
create feature 6 local-clone-invocation
```

creates:

```text
dev/6-local-clone-invocation
```

`<type>` still controls version-bump behavior, even though the branch name does not include it.

### 3. Version behavior

While work is in progress, issue branches use PEP 440 development versions:

```text
1.4.0.dev1+issue-6-local-clone-invocation
```

Version bumps follow branch type:

- `feature` → minor;
- `fix`, `docs`, `refactor`, `test`, `chore` → patch;
- breaking changes use an explicit `--major`.

`finish` replaces the development version with the final release version before merging.

The version source remains project-specific. For this project, the helper uses the existing version-resolution behavior and updates:

```text
forgejo_to_github/__about__.py
```

### 4. Changelog behavior

Add a shared changelog-promotion helper.

It must:

1. require `CHANGELOG.md` to exist;
2. require a non-empty `## [Unreleased]` section;
3. move that content into:

   ```text
   ## [X.Y.Z] - YYYY-MM-DD
   ```

4. insert a new empty `## [Unreleased]` section;
5. leave committing to the lifecycle command.

The existing commit-only `gc_changelog` behavior remains available; it is not the promotion mechanism.

### 5. Finish behavior

A user-invoked `finish` must:

- verify it is on an issue branch;
- require a clean tracked tree;
- run the configured project test command without a skip option;
- require promotable changelog content;
- rebase the issue branch onto the configured base branch;
- promote the changelog;
- replace the development version with the release version;
- commit the changelog and version changes;
- merge into the base branch with a message containing `closes #N`;
- create annotated tag `vX.Y.Z`;
- stop before pushing;
- stop before creating a GitHub release.

`release` provides the same changelog, version, test, commit, and tag behavior directly on the base branch when no issue branch is involved.

### 6. Agent/commit policy

This workflow does not change the agent policy:

- agents never initiate commits, merges, pushes, pulls, fetches, releases, or lifecycle commands;
- agents execute one of these commands only when the user explicitly directs that operation;
- when the user directly invokes the helper, its commits are user-directed automation, not agent-initiated commits;
- pushing and creating the GitHub release remain manual.

### 7. Exit-code contract

The lifecycle command uses:

| Code | Meaning |
|---|---|
| `0` | requested operation completed |
| `1` | usage, validation, or precondition failure |
| `2` | configured project test command failed |

Git-command failures propagate a nonzero status without being reinterpreted as success.

## Project integration

Configure this project through `scripts/project_settings.sh`:

- branch template: `dev/{issue}-{slug}`;
- base branch: `main`;
- test command: `scripts/run-tests.sh`;
- changelog file: `CHANGELOG.md`;
- package/version resolution: existing `ROOT_PACKAGE_NAME`;
- publication: disabled, no PyPI upload.

Update `AGENTS.md` and `README.md` only to document the approved workflow and the agent/use distinction. Do not duplicate the helper implementation in this repository.

## Test contract (RED stage)

`repo-mgmt-scripts` currently has no test suite. Add a lightweight shell/git temporary-repository harness for the new workflow.

It must cover:

- branch-template generation for this project;
- invalid type, issue, and slug rejection;
- development-version calculation and formatting;
- final-version replacement;
- changelog promotion and empty-changelog rejection;
- missing-changelog rejection;
- dirty-tree rejection;
- invocation of the configured test command;
- test-failure exit behavior;
- `finish` merge-message and tagging behavior;
- direct-release tagging behavior;
- no push or remote-release behavior.

No implementation work begins until the issue is filed, the RED tests are approved and committed, and their failures are established for the contract reason.

## Verification

- new shared workflow tests;
- `pre-commit run --all-files` in `repo-mgmt-scripts`;
- `./scripts/run-tests.sh` in `forgejo-to-github`;
- `ruff check .`;
- `mypy f2gh.py forgejo_to_github/`.

## References

- Current branch convention: `AGENTS.md §4`;
- Current manual release process: `AGENTS.md §5`;
- Shared branch/tag helpers: `submodules/repo-mgmt-scripts/src/`;
- Source workflow: `../rag-mcp/scripts/issue-branch.sh`;
- Current changelog: `CHANGELOG.md`.
