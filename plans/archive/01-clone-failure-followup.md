# Clone failure follow-up (f2gh.py:237)

**GitHub issue:** [#2](https://github.com/zcutlip/forgejo-to-github/issues/2)

## Summary

`git clone --mirror` originally blew up with `CalledProcessError` on network failure (exit 128), unlike `push` which already had graceful handling. The initial failure has since been addressed; this plan tracks verification and follow-up behavior.

## Repro

Traceback at `f2gh.py:237` in `mirror_git_repo` line, same as issue #1 but for clone.

```
subprocess.CalledProcessError: Command '['git', 'clone', '--mirror', ...]' returned non-zero exit status 128.
  File "f2gh.py", line 237, in mirror_git_repo
```

## Completed

- Split clone, branch push, and tag push into separate atomic `try` blocks.
- Added graceful `CalledProcessError` handling for `git clone --mirror`.
- Redacted tokens from clone and push errors before displaying them.
- Added sanitized, actionable workflow-scope advice for push failures.
- Kept temporary-directory cleanup in `finally`.
- Made Git push failure non-fatal to issue migration and included Git status in
  the final report.
- Treat clone failure as terminal because the source repository has not been
  successfully validated; `--skip-git` remains the explicit issue-only path.
- Prevented the final report from claiming migration success when Git failed.
- Added clone-specific guidance for common network, authentication, and
  repository-access failures.

## Existing session note #3

Wrap `clone` in the same redaction pattern as `push`, make clone failure
terminal, and keep `state.json` deterministic.

## Remaining work

- Verify clone failures with representative network/authentication errors and
  confirm the displayed message identifies the clone operation and stops before
  issue migration.
- Do not show workflow-scope advice for clone failures.
- Add automated tests for clone failure, push failure, token redaction, continuation to issue migration, and final status reporting.
- Decide whether a failed or interrupted migration should retain a successful mirror for retry; see `plans/04-retain-clone-cache.md`.
- Keep `state.json` deterministic.

## Reference

GitHub issue #1 — https://github.com/zcutlip/forgejo-to-github/issues/1
