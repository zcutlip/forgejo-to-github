# forgejo-to-github

Migrate a repository from [Codeberg](https://codeberg.org) / Forgejo to GitHub — the git history, issues, comments, and labels — with original-author attribution, rate-limit safety, and resumable checkpoints.

## Features

- **Issues, comments, and labels** — migrated in chronological order (pull requests excluded), with the original Codeberg author and date preserved in a blockquote header.
- **Git mirror** — clones the source as a mirror and pushes all branches and tags. The mirror is cached per migration under the platform's user-cache directory (`~/Library/Caches/f2gh/` on macOS, `~/.cache/f2gh/` on Linux), so a resumed run reuses it instead of cloning again. The cache is deleted once the push succeeds; when the push fails it is kept for the retry. `--clean` removes it.
- **Resumable** — progress is checkpointed with atomic writes, so an interrupted migration picks up where it left off instead of duplicating work. State lives outside your working directory, under the platform's user-state directory (`~/Library/Application Support/f2gh/` on macOS, `~/.local/state/f2gh/` on Linux), namespaced per source→target pair and named `state.json`; pass `--state-file PATH` to keep it somewhere else. If you delete the GitHub repo, delete that state file too — otherwise the tool will skip everything the stale state claims is done. There is no flag to reset the checkpoint: delete the file, or point `--state-file` at a fresh path.
- **Rate-limit aware** — backs off with jitter when GitHub throttles rapid POST/PATCH operations.
- **Dry-run mode** — preview exactly what would be created without touching either forge.

## Requirements

- Python 3.12+
- [`gh`](https://cli.github.com/) (optional — used to read your GitHub token if `GITHUB_TOKEN` isn't set)
- A POSIX system (macOS or Linux). The code is written portably and should run on Windows, but it is only validated on POSIX.

## Installation

```bash
# install the `f2gh` command
pipx install git+https://github.com/zcutlip/forgejo-to-github

# ...or into the current environment
pip install .

# ...or run without installing (the script is executable)
./f2gh.py --help
```

## Authentication

| Forge   | Source                                        |
|---------|-----------------------------------------------|
| GitHub  | `GITHUB_TOKEN` env var, or `gh auth token`     |
| Codeberg| `CODEBERG_TOKEN` env var                       |

## Usage

```bash
# Preview first — always
f2gh --source owner/repo --target owner/repo --dry-run

# Run the real migration
f2gh --source owner/repo --target owner/repo
```

From inside a local checkout of the source repo the flags can be omitted:
the source is inferred from the checkout's origin (with one confirm prompt),
and the target defaults to `<your-github-account>/<repo>`. Pass `--cwd` to
skip the confirm. The git mirror is then cloned from the local path rather
than fetched from Codeberg, so a large history isn't transferred a second
time. Codeberg is still contacted to read the issues and comments, and to
check that the checkout is in sync with its origin.

```bash
# From the repo root — infer source, default target, clone locally
f2gh --cwd --dry-run
f2gh --cwd
```

The target repo is created for you (private by default) if it doesn't already exist; the git mirror is pushed before issues are migrated.

### Running from a local checkout

`--cwd` — or an accepted inference prompt — uses the working directory as the
source. Before anything is created, the checkout is checked in order:

1. it is inside a git work tree;
2. it has an `origin` remote, read with `git ls-remote --get-url origin` so
   `url.insteadOf` rewrites apply;
3. that origin points at Codeberg;
4. it is not a shallow checkout;
5. it is not a partial clone.

Any failure is a usage error and the run stops. Shallow checkouts are
rejected because a depth-limited clone already has the remote tip locally, so
a freshness check would pass it and the mirror would then be rejected on
push — after the issues had migrated.

The checkout is then compared against its origin with a read-only
`git ls-remote` (never a fetch, which would modify your working copy):

- every branch and tag on the remote must be present locally and not behind.
  The comparison is per-ref: a local branch whose commits arrived through
  some other ref still counts as diverged, and prompts like any other stale
  state;
- anything behind, diverged, missing locally, or moved upstream produces one
  confirmation prompt listing the refs; declining stops the run before
  anything is created;
- refs that exist only locally are announced and migrate along with
  everything else;
- if the probe itself fails, the run warns and continues.

`--yes` accepts that confirmation automatically, so an unattended run will
migrate stale local state.

Uncommitted changes never block the run; they produce a notice, because only
branches and tags are migrated.

A checkout on a different filesystem than the cache (external storage, say)
copies every object instead of hardlinking it. The cache is released once the
push succeeds.

### Options

| Flag | Description |
|---|---|
| `--source OWNER/REPO` | Source repo on Codeberg (default: inferred from the local checkout) |
| `--target OWNER/REPO` | Target repo on GitHub (default: `<github-account>/<repo>`) |
| `--cwd` | Source the migration from the current checkout; skips the inference prompt |
| `--dry-run` | Preview without making changes |
| `--yes` | Skip confirmation prompts. Requires explicit `--source` and `--target`, and accepts a stale-checkout warning without asking |
| `--skip-git` | Skip the git mirror; migrate issues only |
| `--public` | Create the target repo public (default: private) |
| `--description TEXT` | Repo description on GitHub (default: copied from Codeberg, fallback "Migrated from Codeberg") |
| `--state-file PATH` | Use this checkpoint file instead of the per-migration default |
| `--clean` | Remove this migration's cached mirror and exit. Always needs an explicit `--target`; uses no token and makes no network calls |
| `--version` | Show version and exit |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Migration finished with nothing left to do |
| `1` | Migration ran but something was incomplete — issues or comments failed, or the push failed. Also returned when you decline the source-inference or stale-checkout confirmation |
| `2` | The clone failed, or the invocation was invalid — bad flags, or a working directory that failed the checks above |
| `3` | The checkpoint could not be locked or written, so the run stopped before mutating anything |
| `130` | Interrupted with Ctrl+C. The checkpoint is saved and the resume command is printed |

Because a declined create-repo prompt is treated as a clean stop, `0` can also
mean "declined" for that particular prompt.

## Development

```bash
pip install -e ".[dev]"   # installs type stubs
./scripts/run-tests.sh    # run the test suite (never invoke pytest directly)
ruff check .              # lint
mypy f2gh.py forgejo_to_github/  # type-check
```
