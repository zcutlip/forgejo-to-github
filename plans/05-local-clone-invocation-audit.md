# Plan 05 audit — local-clone invocation

**Subject:** `plans/05-local-clone-invocation.md` (spec draft)
**GitHub issue:** [#6](https://github.com/zcutlip/forgejo-to-github/issues/6)
**Branch:** `dev/6-local-clone-invocation`
**Date:** 2026-09-22
**Audit priorities:** (1) never leave user data in a bad state — source remote,
dest remote, or local; (2) consistency with user expectation — no unpleasant
surprises.

**Verdict:** the core design (validated-cwd → local clone, superset freshness,
reuse of the #5 flow) is sound and its assumptions check out against the
implementation. But there are 8 gaps against the two priorities, two of them
verified by experiment (git 2.54.0). Status: findings open — spec amendments
are the next stage, gated on approval.

Findings cite plan 05 sections by number ("section 4" = `### 4. Target
inference`, etc.).

## Priority 1 — data in a bad state

### 1. Shallow checkouts pass freshness, then fail late and loop forever *(verified, git 2.54.0)*

A `git clone --depth 1` checkout has the remote tip *locally*, so section 5's
binary classification says **equal** and the freshness gate passes silently.
Then: GitHub repo gets created → mirror clone succeeds (itself shallow) →
`push --all` is rejected: `! [remote rejected] main (shallow update not
allowed)`. Because push failure is **non-fatal** (#5 semantics,
migration.py:483), issues then migrate onto an empty repo. The shallow mirror
is retained on push failure, so *every resume re-fails identically* — the only
way out is `--clean` + `git fetch --unshallow`, and the only signal is a
cryptic git stderr line.

Fix: add a section-3 cwd check — `git rev-parse --is-shallow-repository` →
usage error, exit 2 (only when a local clone will happen, same "when" as
section 5). Sibling case: promisor/partial clones (`--filter=blob:none`) —
local test was inconclusive (file:// ignored the filter), but the same check
family covers it (`git config extensions.partialclone`); Codeberg's filter
support is spotty so incidence is low, but a one-line config check is cheap
insurance.

### 2. Target inference can name an account the migration can't act on *(plan section 4 — Target inference)*

Section 4 infers `<gh-user>` from `gh api user`, but the CLI resolves the
*acting* credential as `GITHUB_TOKEN` env **first**, `gh auth token` second
(f2gh.py:226–239). When both exist and differ, inference announces `B/repo`
while `create_repository` POSTs `/user/repos` as the *token* identity A
(github.py:286) → repo lands under A, then pushes and issue-creates against
`B/repo` fail. Classic wrong-account surprise.

Fix: resolve `<gh-user>` from the credential actually used — `GET /user` with
the resolved token — not from the gh CLI. Same cost, removes the gh dependency
for this lookup entirely.

## Priority 2 — expectation consistency

### 3. Freshness probe can hang instead of failing *(plan section 5)*

"Probe failure → warn and continue" assumes `ls-remote` *returns*. Against an
SSH origin with an unknown host key or passphrase-protected key, or an HTTPS
private origin needing credentials, `ls-remote` **blocks on the tty mid-run** —
an unbounded hang, the worst kind of surprise, never reaching the warn path.

Fix: run all read-only probes with `GIT_TERMINAL_PROMPT=0` and ssh
`BatchMode=yes`, converting interactivity into fast failure → warn+continue.
Assert the env in the no-fetch-ever test seam.

### 4. Peeled tag lines false-positive as "moved" *(plan section 5)*

`ls-remote` lists annotated tags twice: `refs/tags/v1` and `refs/tags/v1^{}`.
If classification doesn't exclude peeled lines, every annotated tag compares
its *commit* SHA against the local *tag-object* SHA → "moved upstream" prompt
on **every run, unsatisfiably** — exactly the failure mode section 5's
scope-filter paragraph was written to prevent.

Fix: one sentence — classify only unpeeled `refs/heads/*` / `refs/tags/*`
lines.

### 5. `--cwd` × explicit `--source` contract is unstated

Section 1 defines `--cwd` as bypassing the inference prompt; section 2 says
local clone follows once "cwd validates as the intended source repo." Never
stated: does `--cwd --source A` in a checkout of B win cwd (wrong objects for
slug A → bad dest) or win source (network clone, `--cwd` silently ignored →
surprise)? And if `--yes --cwd --source --target` is permitted, the freshness
behind/moved prompt auto-accepts per "normal --yes rules" — stale state
migrates *unattended*.

Fix: state all three cases — match → local clone, no prompt; mismatch → usage
error, exit 2; `--yes` auto-accept of freshness called out explicitly so it's
a choice, not an accident.

### 6. Local-only-refs notice must precede consent *(plan section 5)*

When behind *and* local-only refs coexist, the unified prompt asks "migrate
local state anyway?" while the local-only list is a separate notice. If the
notice lands after the prompt, the user consents without seeing that e.g.
`secret-branch` is about to be published to GitHub.

Fix: fold the local-only list into the unified prompt text (or emit the notice
first) — one ordering sentence.

### 7. `url.insteadOf` rewrites defeat the origin regex *(plan section 3)*

`git config --get remote.origin.url` returns the *raw* URL; users with
insteadOf shortcuts (`cb:owner/repo`) get a spurious exit-2 despite a genuine
Codeberg origin.

Fix: read the URL via `git ls-remote --get-url origin` (applies insteadOf);
document that ssh-config `Host` aliases stay unsupported.

## Verified, needs a decision (not necessarily a fix)

### 8. `refs/stash` and `refs/remotes/origin/*` ride into the cache mirror *(verified)*

`git clone --mirror` from a checkout copies them; `push --all`/`--tags`
(git.py:611,647) never pushes them, so no remote harm — but on push-failure
the retained cache holds the user's **uncommitted stash** in
`~/.cache/f2gh/...`, which is surprising and mildly sensitive. Options:
document as accepted, or prune non-head/tag refs post-clone (tension with the
pristine-mirror rationale in "Deferred enhancement" — same tradeoff, decide
together).

## Confirmed solid

- Section 2's "untouched #5 flow" matches the code exactly: `clone --mirror`
  takes the local path verbatim (git.py:510), validation is bare + origin-url,
  eviction is scoped to the resolved cache path, `--clean` needs no tokens,
  refuses under lock, and honors dry-run (f2gh.py:174–205).
- Non-tty EOF deny matches the prompter's `EOFError → default(False)`
  (f2gh.py:111–118).
- **Linked worktree as clone source: verified safe** — all refs come through
  the common gitdir.
- All departures from issue #6's text (goal-(a) shorthand, silent network
  fallback, B-direct, bundle) are documented in "Explicitly dropped."

## Minor one-liners

- Match the Codeberg host case-insensitively.
- Dirt notice could say "branches/tags" not "committed refs" (detached-HEAD
  commits don't migrate).
- Add a `--clean --dry-run` + inference test.
- The "Session note #5" reference no longer resolves to anything (notes store
  is empty) — harmless dead pointer.

## Suggested RED additions

Shallow-rejection exit-2; `--cwd`×`--source` match/mismatch; peeled-tag
filtering; probe-env assertion in the no-fetch seam; insteadOf-rewritten
origin acceptance.

## Verification method (evidence trail)

Empirical: throwaway git repos under the sandbox temp dir (git 2.54.0) —
baseline `clone --mirror` from a checkout (carries `refs/stash`,
`refs/remotes/origin/*`); mirror from a linked worktree (works, all refs);
shallow checkout (`--depth 1`): remote tip known locally → freshness classifies
**equal**, mirror-from-shallow is shallow (1 commit), `push --all` rejected
with `shallow update not allowed`; partial-clone test inconclusive locally
(file:// transport ignored `--filter=blob:none`).

Code: f2gh.py (flag parsing, token resolution order, `_run_clean`, prompter),
forgejo_to_github/git.py (clone/push argv, cache validation),
forgejo_to_github/migration.py (push non-fatal, resume lifecycle),
forgejo_to_github/github.py (`/user/repos` vs `/orgs/{owner}/repos`).

Cross-checks: plan 04 (`plans/04-retain-clone-cache.md`) cache contract;
issue #6 body (departures documented in "Explicitly dropped"); session notes
store (empty — dead "Session note #5" reference in both the plan and issue #6).
