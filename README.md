# forgejo-to-github

Migrate a repository from [Codeberg](https://codeberg.org) / Forgejo to GitHub — the git history, issues, comments, and labels — with original-author attribution, rate-limit safety, and resumable checkpoints.

## Features

- **Issues, comments, and labels** — migrated in chronological order (pull requests excluded), with the original Codeberg author and date preserved in a blockquote header.
- **Git mirror** — clones the source as a mirror and pushes all branches and tags.
- **Resumable** — progress is checkpointed to `state.json` with atomic writes, so an interrupted migration picks up where it left off instead of duplicating work.
- **Rate-limit aware** — backs off with jitter when GitHub throttles rapid POST/PATCH operations.
- **Dry-run mode** — preview exactly what would be created without touching either forge.

## Requirements

- Python 3.10+
- [`gh`](https://cli.github.com/) (optional — used to read your GitHub token if `GITHUB_TOKEN` isn't set)

## Installation

```bash
# install the `f2gh` command
pipx install .

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

The target repo is created for you (private by default) if it doesn't already exist; the git mirror is pushed before issues are migrated.

### Options

| Flag | Description |
|---|---|
| `--source OWNER/REPO` | Source repo on Codeberg (required) |
| `--target OWNER/REPO` | Target repo on GitHub (required) |
| `--dry-run` | Preview without making changes |
| `--yes` | Skip interactive prompts (scripting/CI) |
| `--skip-git` | Skip the git mirror; migrate issues only |
| `--public` | Create the target repo public (default: private) |
| `--description TEXT` | Repo description on GitHub (default: copied from Codeberg, fallback "Migrated from Codeberg") |

## Development

```bash
pip install -e ".[dev]"   # installs type stubs
ruff check .              # lint
mypy f2gh.py              # type-check
```
