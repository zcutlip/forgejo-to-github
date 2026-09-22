# Local-checkout sourcing (`--cwd` / omit-plus-confirm + local clone into cache)

**GitHub issue:** [#6](https://github.com/zcutlip/forgejo-to-github/issues/6)
**Branch:** `dev/6-local-clone-invocation`
**Status:** spec — all decisions locked.

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
- `--cwd` × explicit `--source`, three cases: cwd origin matches `--source`
  → local clone, no prompt; mismatch → usage error, exit 2 (never silently
  prefer one — wrong objects for the other slug means a bad destination);
  `--source` alone → today's network flow.
- Guards: `--yes` requires explicit `--source`; non-tty denies inference.
  Called out explicitly: `--yes` combined with cwd sourcing auto-accepts
  the freshness prompt (§5), so a stale checkout can migrate unattended —
  that is a choice the user makes, not an accident.

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
2. `remote.origin.url` resolves — read via `git ls-remote --get-url origin`
   so `url.insteadOf` shortcuts apply (a raw `config --get` would spuriously
   reject `cb:owner/repo`). ssh-config `Host` aliases remain unsupported
   and are documented as such.
3. Origin matches Codeberg patterns (`codeberg.org[:/]owner/repo`, optional
   `.git`, host match case-insensitive).
4. Not a shallow checkout (`git rev-parse --is-shallow-repository`). A
   `--depth` clone has the remote tip locally, so §5 would classify it
   *equal* and wave it through; the shallow mirror is then rejected on push
   (`shallow update not allowed`) while issues still migrate onto an empty
   repo, and the retained cache re-fails identically on every resume.
5. Not a partial/promisor clone (`git config --get extensions.partialclone`
   present). Inconclusive in the local experiment (`file://` ignored
   `--filter`), so one config check is cheap insurance.
6. Absolutize via `Path.resolve()` so the cached mirror's `origin.url`
   has a stable form for resume validation.

Checks 4–5 gate the clone and so apply under the same "when" as §5 (local
clone runs only; never for `--skip-git` or network-clone runs).

### 4. Target inference

- `--target` optional, defaults to `<gh-user>/<source-repo>` where
  `<gh-user>` is resolved from the **credential actually used** — `GET /user`
  with the resolved GitHub token — and repo from the (explicit or inferred)
  source slug. Not from `gh api user`: the CLI prefers `GITHUB_TOKEN` over
  `gh auth token`, and when the two name different accounts, `gh`-derived
  inference would announce one owner while `create_repository` posts as the
  other, landing the repo under an account the subsequent pushes and issue
  creates cannot reach. Same request cost, and it removes the `gh`
  dependency from this lookup.
- `GET /user` failure → require explicit `--target` (graceful, never hard).
- `--yes` requires explicit `--target`.

### 5. Freshness (locked)

The repo-wide superset invariant: everything present remotely is present
locally and in sync; the only allowed exceptions are refs absent remotely
(locally-created branches and tags). Read-only `ls-remote` against the
origin, never fetch (a fetch would mutate the user's checkout and redefine
"local state"). Exact "N commits behind" is uncomputable without the
objects locally, so status is binary per ref. Tip containment implies
object coverage (a remote tip contained in local history brings all its
reachable objects), so no separate object check is needed.

- **Scope:** `refs/heads/*` + `refs/tags/*` only, filtered before
  classifying — forges may advertise synthetic namespaces (`refs/pull/*`,
  etc.) that a clone never carries, and unscoped comparison would prompt
  about them on every run, unsatisfiably. Peeled tag lines
  (`refs/tags/*^{}`, which `ls-remote` emits alongside every annotated tag)
  are excluded too: classifying them would compare the remote *commit* SHA
  against the local *tag-object* SHA and report "moved upstream" on every
  run, the same unsatisfiable-prompt class. Driven by the **remote** ref
  list: each advertised ref must resolve locally (catches remote-only refs
  that same-named-pair comparison would miss).
- **Heads:** equal / ahead-or-diverged (remote tip present locally, tips
  differ) / behind (remote tip unknown locally).
- **Tags follow the superset rule:** local tags must cover all remote tags.
  Missing-locally or moved (same name, different SHA) joins the prompt
  below. Local-only tags are allowed and migrate with everything else
  (announced, never blocked) — uniform with ahead branches: all local refs
  migrate.
- **Policy:** any behind/missing/moved → single unified prompt via the
  prompter seam ("N branches behind, tag v1.3 missing locally, tag v2.0
  moved upstream — migrate local state anyway?"), deny aborts before
  anything mutates. When local-only refs coexist, their list is folded into
  that same prompt text so consent covers what will be published — a
  consent prompt that omits "branches [secret-branch] will migrate" is
  consent to something the user never saw. Ahead/local-only only → one
  combined announce-and-proceed notice ("local-only refs that will
  migrate: branches […], tags […]").
- **When:** only when a local clone will actually happen. Skipped under
  `--skip-git` (no objects needed — staleness must not block an
  issues-only run) and for network-clone runs (nothing local to check).
- **Probe failure → warn and continue** (the API phases fail fast on their
  own if the network is truly down). The probes run with
  `GIT_TERMINAL_PROMPT=0` and ssh `BatchMode=yes` so that an SSH host-key
  or passphrase prompt, or an HTTPS private origin needing credentials,
  fails fast instead of blocking on the tty mid-run — an unbounded hang is
  the one outcome the warn path cannot reach.
- **`--yes` interaction:** with `--yes` (and cwd sourcing), this prompt
  auto-accepts, so stale local state migrates unattended. Explicit and
  intentional (§1); fresh checkouts should be the norm under automation.
- **Rationale:** full offline is incoherent (issues/GitHub APIs need
  network regardless); the probe's purpose is catching stale checkouts,
  and it costs kilobytes.

### 6. Dirt — courtesy-only (locked)

`git status --porcelain` notice only ("uncommitted changes present; only
branches and tags migrate" — not "committed refs", since a detached-HEAD
commit is committed yet not on any branch). Never blocking, never gating —
uncommitted work is unreachable from refs so it cannot affect the
migration; the notice closes the expectation gap only.

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
- **Extra refs in the retained cache** (verified): `git clone --mirror` from
  a checkout also carries `refs/stash` and `refs/remotes/origin/*`.
  `push --all`/`--tags` never send them, so nothing reaches GitHub; the
  residue is local only (the user's own stash under `~/.cache/f2gh/…` on a
  push failure, bounded by success-delete and `--clean`). Documented as
  accepted rather than pruned: pruning would add a deletion step to the
  highest-stakes path (the clone) with its own failure policy, and would
  soften the pristine-mirror invariant, to remove narrow local residue on
  the user's own disk. Revisit if the cache directory ever becomes shared
  or synced.

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
  `.git`-suffixed / SSH forms / case-varied host) + exit-2 messages.
- `insteadOf`-rewritten origin (`cb:owner/repo`) accepted; ssh `Host` alias
  documented unsupported.
- Shallow checkout → exit 2; partial clone (`extensions.partialclone`) →
  exit 2.
- Omit-plus-confirm accept/deny; `--cwd` skips inference prompt; `--cwd` ×
  `--source` match → local clone / mismatch → exit 2; `--yes` requires
  explicit `--source`/`--target`; non-tty denies.
- Target inference resolves `<gh-user>` from `GET /user` with the resolved
  token (mocked), including the token-vs-`gh` divergence case; lookup
  failure → explicit `--target` required.
- Announcement lines; ahead/behind/dirt notices (binary per-ref status,
  peeled tag lines excluded from classification, unified
  behind/missing/moved prompt with deny-aborts, local-only list folded into
  the prompt when both coexist, combined local-only-refs notice,
  probe-failure warning).
- Probe env assertion (`GIT_TERMINAL_PROMPT=0`, ssh `BatchMode=yes`) inside
  the no-fetch-ever seam.
- Absolutize-stability; form-mismatch resume → fresh clone; no-fetch-ever
  assertion (scripted runner rejects mutating argv); dry-run messaging;
  `--clean` with inference; `--clean --dry-run` + inference.
- Existing explicit-invocation suite passes unchanged.

## References

- Issue #1
- Plans `04-retain-clone-cache.md` (#5 — the cache flow reused verbatim)
- Audit `05-local-clone-invocation-audit.md` (findings 1–8 folded in)
