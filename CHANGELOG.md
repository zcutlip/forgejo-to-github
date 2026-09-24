# Changelog

All notable changes to `f2gh` are documented here.

Changes to exit-status contracts are recorded under `Changed`.

## [Unreleased]

### Added

- Support migrating directly from a local checkout.
- Infer the source repository from the current directory.
- Default the target repository to the authenticated GitHub user.
- Add local-checkout freshness checks before migration.
- Add explicit handling for divergent, missing, and moved local refs.
- Support `--cwd` for selecting the local checkout used as the migration source.
- Ensure `--yes` covers all migration confirmations.

### Changed

- Failed clones now exit with status `4` instead of sharing status `2` with invalid invocations.
- Declined confirmations now exit with status `5` instead of `1` (or `0` for the target-repository prompt).
- Interrupted migrations now exit with status `130` after printing a clean message instead of a traceback.

### Fixed

- Handle `Ctrl+C` cleanly when interruption occurs before state-path resolution.

## [1.3.0] - 2026-09-17

### Added

- Persist clone paths alongside migration state.
- Retain the cloned repository cache between runs.
- Add `--clean` to remove the clone cache after successful completion.
- Report clone-cache lifecycle progress.
- Delete cached clones after a successful push while preserving them for recovery when a run fails.

## [1.2.0] - 2026-09-15

### Added

- Store migration state outside the current working directory using platform-specific user state directories.
- Namespace state files by source and target repository.
- Add support for an explicit state-file override.
- Add state-file locking to prevent concurrent migrations from corrupting state.
- Add state and lock-file preflight checks.
- Persist whether the target repository was created.

### Changed

- Migration runs now abort if their state cannot be persisted.
- State preflight failures now exit with status `3`.
- State updates use durable atomic writes.

## [1.1.0] - 2026-09-13

### Added

- Graceful handling of `KeyboardInterrupt`.
- Cleanup of temporary clone directories when cloning fails.

### Changed

- Improved migration test assertions and removed vacuous test cases.

## [1.0.1] - 2026-09-12

### Fixed

- Cap GitHub rate-limit retry delays at 60 seconds.
- Clamp negative `Retry-After` values to a one-second minimum.
- Clarify the distinction between `--version` and `--help`.

## [1.0.0] - 2026-09-08

### Added

- Initial release of `f2gh`.
- Migrate issues, comments, and labels from Codeberg or Forgejo to GitHub.
- Create target repositories and mirror Git branches and tags.
- Preserve original issue links and comment attribution.
- Copy repository descriptions by default.
- Support dry-run migration previews.
- Add request timeouts and client-side issue ordering.
- Add resumable migration checkpoints.
- Add pre-flight target-repository checks.
- Add GitHub rate-limit retry handling with jitter and proactive throttling.
- Make clone failures terminal while allowing Git push failures to continue issue migration.
- Track and report migration failures and skipped items.
