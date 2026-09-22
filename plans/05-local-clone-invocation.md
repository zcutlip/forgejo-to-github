# Local-checkout sourcing (`--cwd` / omit-plus-confirm + local clone into cache)

**GitHub issue:** [#6](https://github.com/zcutlip/forgejo-to-github/issues/6)
**Branch:** `dev/6-local-clone-invocation`
**Status:** spec draft — decisions below are locked unless marked **OPEN**.

## Context

Typical invocation from inside local clone:

```bash
f2gh --source zcutlip/"$(basename $(pwd))" --target zcutlip/"$(basename $(pwd))" --public
```

User runs from repo root, `source`/`target` derived from `basename $(pwd)`,
but currently requires explicit `--source`/`--target` and always does
`git clone --mirror` from Codeberg even though `cwd` already has the full repo.

Original intent (locked): if we have the project locally we can skip having
to provide the source project and skip cloning the repo *over the network*.
Issues/metadata still come from the Forgejo API — only the *clone* goes local.

## Locked contract

### 1. Slug sourcing (A3 + explicit `--cwd`)

- `--source` becomes optional. When missing, infer `owner/repo` from the
  cwd's `remote.origin.url` (must match Codeberg SSH/HTTPS patterns,
  optional `.git` suffix — non-match is a usage error, exit 2, since the
  issues API needs a Codeberg slug).
- Omit-plus-confirm: inference stops with a hard prompt before anything
  mutates (`Inferred source X from <path> — proceed? [y/N]`). Wrong
  directory becomes a No, not a migration. Non-tty EOF denies.
- Optional `--cwd`: explicit opt-in to cwd inference that **bypasses the
  inference prompt only** (intent already expressed). It replaces `--yes`
  for that one prompt — create-repo, freshness, and all other prompts still
  follow normal `--yes` rules.
- Guards: `--yes` requires explicit `--source`; non-tty denies inference.

### 2. Validated cwd implies local objects (A and B re-coupled)

- Once cwd validates as the intended source repo (via `--cwd` or accepted
  inference prompt), **B-local follows automatically**: clone from the
  absolutized local path into the per-migration cache. No second trigger,
  no B-network-after-inference path.
- Network clone remains only for non-local runs (explicit `--source`, no
  valid cwd involved). Existing explicit invocations work unchanged.
- Everything downstream is the untouched #5 flow: checkpoint `clone_path`,
  validate (bare + origin), evict-before-reclone, push, `--clean`,
  delete-on-success / keep-on-push-failure, `git_pushed` semantics.
  `GitMirror.source_url` is just argv — a local path works verbatim.

### 3. Cwd checks (in order, first failure is usage error, exit 2)

1. Inside a work tree (`git rev-parse --is-inside-work-tree`).
2. `remote.origin.url` exists.
3. Origin matches Codeberg patterns (`codeberg.org[:/]owner/repo`,
   optional `.git`).
4. Absolutize via `Path.resolve()` so the cached mirror's `origin.url`
   has a stable form for resume validation.

### 4. Target inference

- `--target` optional, defaults to `<gh-user>/<source-repo>` where
  `<gh-user>` comes from `gh api user --jq .login` and repo from the
  (explicit or inferred) source slug.
- `gh` lookup failure → require explicit `--target` (graceful, never hard).
- `--yes` requires explicit `--target`.

### 5. Freshness (locked)

Read-only `ls-remote` against the origin, never fetch (a fetch would mutate
the user's checkout and redefine "local state"). Exact "N commits behind"
is uncomputable without the objects locally, so status is binary per ref:

- **Scope:** all heads. Each local branch vs same-named remote ref:
  equal / ahead-or-diverged (remote tip present locally, tips differ) /
  behind (remote tip unknown locally).
- **Tags follow the superset rule:** local tags must cover all remote tags.
  Missing-locally or moved (same name, different SHA) joins the prompt
  below. Local-only tags are allowed and migrate with everything else
  (announced, never blocked) — uniform with ahead branches: all local refs
  migrate.
- **Policy:** any behind/missing/moved → single unified prompt via the
  prompter seam ("N branches behind, tag v1.3 missing locally, tag v2.0
  moved upstream — migrate local state anyway?"), deny aborts before
  anything mutates. Ahead/local-only → one combined announce-and-proceed
  notice ("local-only refs that will migrate: branches […], tags […]").
- **Probe failure → warn and continue** (the API phases fail fast on their
  own if the network is truly down).
- **Rationale:** full offline is incoherent (issues/GitHub APIs need
  network regardless); the probe's purpose is catching stale checkouts,
  and it costs kilobytes.

### 6. Dirt — courtesy-only (locked)

`git status --porcelain` notice only ("uncommitted changes present; only
committed refs migrate"). Never blocking, never gating — uncommitted work
is unreachable from refs so it cannot affect the migration; the notice
closes the expectation gap only.

### 7. Announcements (stdout)

- `Using local checkout <path> as clone source (origin <url>)` plus
  ahead/behind/dirt notices as applicable.
- Dry-run reports the inferred slug + `would clone from <path>`; no
  subprocess beyond the read-only probes.
- `--clean` accepts `--cwd`/inference for slug resolution.

### 8. Resume / forms / semantics

- Cached mirror's `origin.url` is the absolutized local path; a later
  `--source` (URL-form) resume mismatches → fresh clone (safe direction).
- Semantics (documented): local sourcing migrates your *local* state, which
  may differ from Codeberg's.

## Explicitly dropped

- **B-direct** (push `--all`/`--tags` straight from cwd, no clone):
  saves only a seconds-long local copy while losing the frozen-snapshot
  invariant #5's resume rests on. Dropped, not deferred.
- **`git bundle`**: copies all objects into a pack file (no hardlinks),
  then clone bundle → cache adds a second copy — worse on disk than B-local
  everywhere, ties nowhere. Dropped.
- **Goal (a) shorthand `f2gh --public` inferring both**: superseded —
  `--target` defaults per §4 instead of being inferred from cwd alone.
- **Silent fallback to network clone**: with explicit intent (`--cwd` or
  accepted prompt), failure is a usage error (exit 2), never a silent
  fallback.

## Deferred enhancement (not in scope)

- **Local-only tag exclusion**: dropping local-only tags from the mirror
  before push. Decided against for now — it would mutate the pristine-mirror
  invariant and need recompute-or-persist across resume, all to avoid
  cosmetic tag-namespace noise. Revisit with evidence of real pain.

## Accepted edge (document, don't fix)

- **Cross-volume local clone**: hardlinks apply only on the same filesystem
  (plain path form, never `file://` URL). Cwd on external storage → full
  copy (transient on success, retained on push failure). Accept and document;
  `B-direct` is not revived for this edge.

## Advisory — SSH push path (contingent on local checkout)

Preserved as future work. The SSH-based workaround advisory stays contingent
on working from a local checkout of the source repo; validated-cwd
simplifies its future detection (known rather than detected) instead of the
`is_local_checkout` probe sketched here before:

```
  2) git remote add github git@github.com:OWNER/REPO.git
     git push github --all
     git push github --tags
     f2gh --source SRC --target DST --skip-git
     Note: --all pushes only local branches...
```

## Tests (RED, separate gate)

- Cwd-check matrix (non-repo / no-origin / non-Codeberg-origin /
  `.git`-suffixed / SSH forms) + exit-2 messages.
- Omit-plus-confirm accept/deny; `--cwd` skips inference prompt; `--yes`
  requires explicit `--source`/`--target`; non-tty denies.
- Announcement lines; ahead/behind/dirt notices (binary per-ref status,
  unified behind/missing/moved prompt with deny-aborts, combined
  local-only-refs notice, probe-failure warning).
- Absolutize-stability; form-mismatch resume → fresh clone;
  no-fetch-ever assertion (scripted runner rejects mutating argv);
  dry-run messaging; `--clean` with inference.
- Existing explicit-invocation suite passes unchanged.

## References

- Issue #1
- Session note #5
- Plans `04-retain-clone-cache.md` (#5 — the cache flow reused verbatim)
