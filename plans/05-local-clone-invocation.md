# Local clone invocation simplification & avoid redundant clone

**GitHub issue:** [#6](https://github.com/zcutlip/forgejo-to-github/issues/6)

## Context

Typical invocation from inside local clone:

```bash
f2gh --source zcutlip/"$(basename $(pwd))" --target zcutlip/"$(basename $(pwd))" --public
```

User runs from repo root, `source`/`target` derived from `basename $(pwd)`, but currently requires explicit `--source`/`--target` and always does `git clone --mirror` from Codeberg even though `cwd` already has the full repo.

## Goals

(a) **Simplify invocation when run from local clone** — e.g., auto-detect `owner/repo` from `git config --get remote.origin.url` or `basename $(pwd)` if origin matches Codeberg pattern `ssh://codeberg.org/...` or `https://codeberg.org/...`, allow shorthand forms:

- `f2gh --public` (infer both source and target from local clone)
- `f2gh --target zcutlip/foo` (infer source from local clone)

(b) **Avoid redundant clone** — if `cwd` is a valid git repo with the source remote, push directly from local (`git push --all` / `--tags` to target) or use `git bundle` / local tmpdir seeded from `cwd` instead of network `git clone --mirror`, saving time and handling offline/network failure gracefully.

## Considerations

- Detect `cwd` is a git repo: `git rev-parse --is-inside-work-tree` (exit 0 = inside work tree).
- Parse `remote.origin.url`: confirm it matches requested source (or inferred source) and matches Codeberg host patterns (`codeberg.org[:/]owner/repo` with optional `.git` suffix).
- Fallback to remote `git clone --mirror` if not a repo, or origin does not match requested source, or parsing fails.
- Preserve deterministic state handling (`state.json` atomic writes via `os.replace`).
- Explicit flags override auto-detect — `--source`/`--target` when given take precedence; auto-detect only fills missing values.
- Keep behavior non-breaking: existing explicit invocations must continue to work unchanged.

## Advisory — SSH push path (contingent on local checkout)

The SSH-based workaround advisory below is **contingent on working from a local checkout** and must only be shown when `f2gh` detects it is running from inside a local checkout of the source repo:

```
  2) git remote add github git@github.com:OWNER/REPO.git
     git push github --all
     git push github --tags
     f2gh --source SRC --target DST --skip-git
     Note: --all pushes only local branches...
```

- **Show when** `is_local_checkout == true`: `git rev-parse --is-inside-work-tree` succeeds (true) **and** `remote.origin.url` matches `source` (Codeberg host patterns `codeberg.org[:/]owner/repo` with optional `.git` suffix).
- **Otherwise, do not show** — fall back to the `gh auth refresh -h github.com -s workflow` path or the full `git clone --mirror` workaround.

> Implementation note: advisory selection logic should branch on `is_local_checkout` (reuse `is_cwd_valid_source_mirror(source)` / `resolve_source_target_from_cwd()`). This contingency is for future implementation — keep existing explicit invocations non-breaking.

## Next steps

- Design CLI flag like `--from-cwd` or auto-detect with `--source auto` — discuss UX tradeoff (explicit opt-in vs. implicit inference).
- Prototype `resolve_source_target_from_cwd()` helper: returns inferred `(owner, repo)` or `None`.
- Prototype `is_cwd_valid_source_mirror(source)` check: verifies `cwd` tracks the expected Codeberg remote.
- Evaluate push strategy: direct `git push --mirror` from `cwd` vs. `git bundle create` + local `--mirror` tmpdir.
- Add dry-run messaging to indicate when local clone path is used vs. network clone.

## References

- Issue #1
- Session note #5
