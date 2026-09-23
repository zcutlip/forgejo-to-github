# Plan 05 audit — local-clone invocation

**Subject:** `plans/05-local-clone-invocation.md` (spec draft)
**GitHub issue:** [#6](https://github.com/zcutlip/forgejo-to-github/issues/6)
**Branch:** `dev/6-local-clone-invocation`
**Date:** 2026-09-22
**Audit priorities:** (1) never leave user data in a bad state — source remote,
dest remote, or local; (2) consistency with user expectation — no unpleasant
surprises.

**Status (current spec):** the original eight findings (plus minor
one-liners) below are addressed in `plans/05-local-clone-invocation.md`:
findings 1–7 fixed, finding 8 explicitly accepted/documented rather than
fixed. Two NEW open findings (A/B) against the current spec follow below.

**Verdict (original audit, 2026-09-22 — preserved as history):** the core
design (validated-cwd → local clone, superset freshness, reuse of the #5
flow) is sound and its assumptions check out against the implementation. But
there are 8 gaps against the two priorities, two of them verified by
experiment (git 2.54.0). Status at the time: findings open — spec amendments
are the next stage, gated on approval.

What follows are the original/draft findings and evidence, preserved as a
historical record — they do not reflect open work against the current spec.
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

---

## Current-spec audit (2026-09-22 — open findings against current spec)

> The sections above are the original 2026-09-22 audit, preserved as history
> (status: findings 1–7 fixed, finding 8 accepted/documented).
> What follows is new, open work against the current spec. Original
> findings/evidence above are preserved; only the header status line was
> updated to point at the new findings.
>
> Recommendation status (2026-09-22): the user authorized DOCUMENTING the
> recommendations for findings A/B below in this audit file only
> (recommendations, not spec decisions). Spec adoption is explicitly out
> of scope — `plans/05-local-clone-
> invocation.md`, tests, and all other files are unchanged by this update.

### A. Freshness "tip object present locally" does not prove same-name branch containment (plan §5, lines ~99–119)

**Spec citation:** plan §5 classifies a head as equal / ahead-or-diverged
whenever the remote tip object is present locally and differs from the local
tip. Presence is checked at the object level (e.g. `git cat-file -e <sha>` /
equivalent "object exists locally"), not as containment by the same-name
local branch.

**Failure mechanism (concrete):** object existence is a repository-global
property, not a per-branch property. Any ref can introduce the object:

1. Codeberg `main` tip = B.
2. Local `main` tip = A, where B is *not* in `main`'s history (diverged or
   simply stale on a different lineage).
3. Some *other* local ref contains B — e.g. another local branch, a stale
   `refs/remotes/origin/main`, a tag, or even `refs/stash` / reflog-adjacent
   state carried into the checkout.
4. Freshness sees "B exists locally, A ≠ B" → labels `main`
   ahead-or-diverged (or equal-path equivalent), satisfying the superset /
   freshness invariant on paper.
5. The run silently proceeds to `git clone --mirror` from the checkout and
   then `push --all`, publishing local `main` = A to the fresh GitHub repo —
   **without remote `main` history B**. The user asked to migrate Codeberg
   state; they get a repo whose `main` has silently dropped the upstream tip
   they never had locally on that branch. No prompt fires because the
   classification already concluded the local side "has" the remote tip.

This is Priority 1 (bad-state: dest content) and Priority 2 (silent wrong
content, no consent) simultaneously. The invariant the spec *means* is
per-branch containment — "same-name local branch contains the remote tip" —
but the check it *writes* is object existence.

**Recommendation (documented only, not adopted into spec):** each
remote branch tip must be an ancestor of the SAME-NAMED local branch (e.g.
`git merge-base --is-ancestor <remote-tip> <local-branch-tip>` per head).
A branch whose remote tip is not contained in its same-name local branch is
divergent / not-contained (never equal, never "ahead" in the safe-to-proceed
sense). Include each such branch in the EXISTING unified freshness
warning/consent prompt — do not add a second prompt. Deny aborts before any
mutation (cache clone, GitHub repo creation, mirror push); `--yes` retains
its explicitly documented auto-accept for this prompt, so unattended
migration of not-contained state remains an explicit choice, not an
accident. The alternative (a separate second prompt sequenced alongside the
unified prompt) is noted but NOT recommended — it adds prompt fatigue with
no safety gain over folding into the existing prompt.

**Decision needed (retained alternative for the record; preference above is unambiguous):** amend §5 so the freshness invariant is satisfied only by
same-name local branch containment (e.g. `git merge-base --is-ancestor <remote-tip> <local-branch-tip>` /
equivalent per-head check), not by bare object presence. Object-present-but-
not-contained must be treated as divergent / not-contained, not folded into
ahead-or-diverged as a safe case. Specify:

- Classification: a branch whose remote tip object exists somewhere locally
  but is not an ancestor of the same-name local branch is **divergent /
  not-contained** (never equal, never "ahead" in the safe-to-proceed sense).
- Behavior: gate before any mutation (cache clone, GitHub repo
  creation, mirror push) via the EXISTING unified freshness prompt — the prompt must name
  the branch, state that the remote tip is not contained in the same-name
  local branch, and state the consequence (proceeding publishes local tip
  without remote tip history). No second prompt. `--yes` auto-accept semantics for this prompt
  must be called out explicitly (as with finding 5's `--yes` rule), so
  unattended migration of not-contained state is a choice, not an accident.
- Scope: per-head (heads only, unpeeled lines per finding 4), not a single
  global gate — one divergent head must not be masked by another head that
  does contain its tip.

**Suggested RED tests (per recommendation — fold into the existing unified prompt):**

- Not-contained via other branch: local `main` = A (no B in history),
  `other` = B (or `refs/remotes/origin/main` = B); remote `main` = B →
  expect divergent/not-contained classification and inclusion in the EXISTING
  unified pre-mutation consent prompt; declining aborts before clone/repo-create; accepting is
  required to proceed.
- Control: local `main` descendant of B (B in `main` history) → freshness
  passes with no not-contained prompt (guards against over-blocking).
- Prompt-count assertion: not-contained scenario emits exactly one consent
  prompt before any mutation — the existing unified freshness prompt with the
  not-contained branch folded in; no second prompt.

### B. `--clean --cwd` without `--target` cannot be both tokenless/offline and target-inferred (plan §4 vs §7, `f2gh.py` ~174–205)

**Spec/code citations:**

- Plan §4: optional `--target` resolves its default (`<gh-user>/<repo>`) by
  authenticated GitHub `GET /user` — a tokened network call under the
  *acting* credential (the finding-2 fix direction).
- Plan §7: `--clean` accepts cwd inference (i.e. `--clean --cwd` resolves
  source — and hence cache/state namespace inputs — from the checkout).
- `f2gh.py` `_run_clean` doc + implementation (~174–205): `--clean`
  guarantees **no tokens, no network** — refuses under lock, honors dry-run,
  needs no credentials (confirmed-solid bullet above).

**Contradiction (concrete):** cache and state namespaces need the target
(eviction is scoped to the resolved cache path; state is per source/target
pair). Invocation `--clean --cwd` without `--target` therefore has only two
possible behaviors, and each breaks one of the two cited guarantees:

1. Preserve tokenless/offline `--clean` → it cannot call `GET /user` to infer
   `<gh-user>`, so it cannot resolve the target namespace from cwd alone.
   Target inference by authenticated lookup is impossible without violating
   the guarantee.
2. Infer target by `GET /user` per §4 → `--clean` now requires a token and
   network, contradicting the `_run_clean` no-tokens/no-network guarantee and
   the confirmed-solid audit bullet. Offline use, credential-less cleanup, and
   the `--clean --dry-run` + inference test (minor one-liners) all change
   meaning.

There is no third option within the current text: cwd inference supplies the
source side, not the GitHub username, so the target half of the namespace
remains unresolvable tokenlessly.

**Recommendation (documented only, not adopted into spec — unambiguous preference for Option 1):** require explicit `--target` for ALL `--clean` invocations. `--cwd` can still supply the source side, but `--clean --cwd` lacking `--target` is an exit-2 usage error naming the missing `--target`. This preserves offline / token-free cleanup and scoped cache selection (eviction stays scoped to the resolved cache path with no `GET /user` lookup). The alternative (Option 2 below — authenticated `GET /user` lookup inside `--clean`) is noted but NOT recommended.

**Policy decision needed (explicit, one of; Option 1 recommended — retained for the record):**

- Option 1 (preserve guarantee — RECOMMENDED): `--clean` requires explicit `--target` for ALL `--clean` invocations. `--clean --cwd` without `--target` → usage
  error, exit 2, with a message naming the missing `--target`. Tokenless /
  offline behavior unchanged; `--cwd` still supplies source.
- Option 2 (allow lookup — NOT recommended): `--clean` may perform the authenticated `GET
  /user` target lookup when `--target` is absent. Then the spec must amend §7
  *and* the `_run_clean` guarantee: document that `--clean` without
  `--target` requires a token + network, specify the failure mode when
  offline / token absent (clean error, exit code, no partial eviction), and
  update the `--clean --dry-run` + inference test expectation accordingly.

Either way, §4 and §7 must agree on which one `--clean` is; today each
implies the other is false.

**Suggested RED tests (per recommendation — Option 1; Option 2 cases retained only as the non-recommended alternative):**

- Recommended (Option 1): `--clean --cwd` without `--target` → exit 2 usage error,
  asserting no token access and no network (extend the existing probe-env /
  no-fetch-ever seam style: fail the test if `GET /user` or any token
  resolution is attempted). Applies to ALL `--clean` invocations lacking `--target`, not just `--cwd`.
- Non-recommended alternative (Option 2, for the record only): `--clean --cwd` without `--target` performs exactly one
  authenticated `GET /user` with the resolved acting credential; plus
  offline/no-token case → clean error (specified exit code) with no partial
  cache/state deletion.
- Both options: `--clean --cwd --target <explicit>` remains fully tokenless /
  offline (no `GET /user` attempted) — locks in that explicit-target cleanup
  never regresses into authenticated behavior.
