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

It supports the full source lifecycle:

```text
create <type> <issue-number> <short-slug>
resume <type> <issue-number> <short-slug>
status
bump-version [--major|--minor|--patch]
finish [--skip-tests] [--skip-changelog]
release [--major|--minor|--patch] [--no-bump] [--skip-tests] [--skip-changelog]
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

`create` and `resume` must start from `BASE_BRANCH`. This enforces the documented create-from-main behavior; the source helper documented it for creation without enforcing it.

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

`resume` reuses the same template and version calculation as `create`, but checks out the existing branch instead of creating a new one. `bump-version` defaults to the branch-type bump level unless an explicit `--major`, `--minor`, or `--patch` option is supplied.

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

### 5. Finish and release behavior

A user-invoked `finish` has two modes.

Normal `finish` must:

- verify it is on an issue branch;
- reject uncommitted tracked changes;
- warn interactively about untracked files;
- run the configured project test command unless `--skip-tests` is supplied;
- require promotable changelog content unless `--skip-changelog` is supplied;
- rebase the issue branch onto the configured base branch;
- promote the changelog;
- replace the development version with the release version;
- commit the changelog and version changes;
- merge into the base branch with a message containing `closes #N`;
- create annotated tag `vX.Y.Z`;
- stop before pushing;
- stop before creating a GitHub release.

Post-merge `finish` recovery applies when `finish` is invoked on the base branch and the latest base-branch commit message closes an issue. It must:

- create no additional merge;
- report that tagging is complete if the release tag already exists;
- prompt before converting a development version into a release version and tagging it;
- prompt before tagging an already-final release version;
- stop before pushing.

`release` requires the base branch and provides changelog, version, test, commit, and tag behavior directly on the base branch when no issue branch is involved. It supports `--major`, `--minor`, `--patch`, `--no-bump`, `--skip-tests`, and `--skip-changelog`. It does not fetch; the user ensures the base branch is current before invoking it.

### 5A. Validation matrix

| Command | Branch precondition | Tree precondition | Tests | Changelog | Remote behavior |
|---|---|---|---|---|---|
| `create` | current branch must equal `BASE_BRANCH`; reject duplicate branch names | clean tracked tree | not run | not required | none |
| `resume` | current branch must equal `BASE_BRANCH`; branch must already exist | clean tracked tree | not run | not required | none |
| `status` | current branch must match the issue-branch pattern | untracked files reported, never blocking | not run | readiness reported, never mutated | nonfatal remote refresh permitted; never mutates branches, versions, changelog, or tags; never pushes |
| `bump-version` | refuse `BASE_BRANCH`; preserve issue metadata from the branch name | clean tracked tree | not run | not required | none |
| `finish` | issue-branch pattern for normal finish; base branch plus qualifying merge message for recovery | reject uncommitted tracked changes; warn interactively about untracked files | required unless `--skip-tests` | required unless `--skip-changelog` | fetch/rebase/merge permitted only as part of the explicitly invoked operation; never push |
| `release` | current branch must equal `BASE_BRANCH` | reject uncommitted tracked changes | required unless `--skip-tests` | required unless `--skip-changelog` | no fetch or push |

The clean-tree requirement for `create`, `resume`, and `bump-version` is deliberate hardening: it prevents unrelated staged changes from being swept into an automated version-bump commit. The branch-template, configurable paths, POSIX implementation, and portable changelog handling are adaptations; no lifecycle command is omitted.

### 6. Agent/commit policy

This workflow does not change the agent policy:

- agents never initiate commits, merges, pushes, pulls, fetches, releases, or lifecycle commands;
- agents execute one of these commands only when the user explicitly directs that operation;
- agents must never supply `--skip-tests` or `--skip-changelog`; those options remain available only for direct user invocation;
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
- `create` refusal outside the base branch;
- duplicate-branch rejection;
- `resume` checkout and version-bump behavior;
- `resume` refusal for a missing branch;
- development-version calculation and formatting;
- final-version replacement;
- `bump-version` refusal on the base branch;
- `status` non-mutating behavior;
- changelog promotion and empty-changelog rejection;
- missing-changelog rejection;
- uncommitted-tracked-change rejection;
- untracked-file warning for `finish`;
- invocation of the configured test command;
- test-failure exit behavior;
- `--skip-tests` and `--skip-changelog` behavior for direct user invocation;
- `finish` merge-message and tagging behavior;
- post-merge tag-recovery behavior without creating another merge;
- release bump-level and `--no-bump` behavior;
- direct-release tagging behavior;
- no fetch behavior for `release`;
- no push or remote-release behavior.

No implementation work begins until the issue is filed, the RED tests are approved and committed, and their failures are established for the contract reason.

## Out of scope

`git-rename-tag.sh` is a separate utility, not part of the issue-branch lifecycle, so it is not included in this slice. This project already has its own test runner.

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
