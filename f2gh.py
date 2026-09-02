#!/usr/bin/env python3

import argparse
import os
import subprocess
import sys
from pathlib import Path

from forgejo_to_github.codeberg import CodebergClient
from forgejo_to_github.domain import Repository
from forgejo_to_github.git import GitMirror
from forgejo_to_github.github import GitHubClient
from forgejo_to_github.migration import MigrationOrchestrator
from forgejo_to_github.reporting import Reporter
from forgejo_to_github.state import StateStore
from forgejo_to_github.transport import RequestsTransport


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate repos from Codeberg/Forgejo to GitHub."
    )
    parser.add_argument(
        "--source",
        required=True,
        metavar="OWNER/REPO",
        help="Source repo on Codeberg (e.g. 'myuser/myproject')",
    )
    parser.add_argument(
        "--target",
        required=True,
        metavar="OWNER/REPO",
        help="Target repo on GitHub (e.g. 'myuser/myproject')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration without making any changes",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip all interactive prompts (for scripting/CI)",
    )
    parser.add_argument(
        "--skip-git",
        action="store_true",
        help="Skip git mirror clone/push (only migrate issues)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Create target repo as public (default: private)",
    )
    parser.add_argument(
        "--description",
        metavar="TEXT",
        default=None,
        help='Repo description on GitHub (default: copied from Codeberg, fallback "Migrated from Codeberg")',
    )
    return parser.parse_args()


def _build_orchestrator(args: argparse.Namespace) -> MigrationOrchestrator:
    """Construct the production :class:`MigrationOrchestrator`.

    Performs all collaborator construction that :func:`main` delegates
    to. Reads tokens from the environment (with ``gh auth token``
    fallback for GitHub), validates ``source``/``target`` shape,
    and builds the five collaborators plus the :class:`Repository`
    value object.
    """
    # State file path — no --state-file flag in this plan.
    state_path = Path("state.json")

    # Codeberg token — required.
    codeberg_token = os.getenv("CODEBERG_TOKEN")
    if not codeberg_token:
        raise SystemExit("CODEBERG_TOKEN not set.")

    # GitHub token — env var preferred, then gh CLI fallback.
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=True,
            )
            github_token = result.stdout.strip()
            if not github_token:
                raise SystemExit("GITHUB_TOKEN not set and 'gh auth token' failed.")
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise SystemExit("GITHUB_TOKEN not set and 'gh auth token' failed.")

    # Validate source/target shape — must be OWNER/REPO.
    source = str(getattr(args, "source", ""))
    target = str(getattr(args, "target", ""))
    for label, value in (("source", source), ("target", target)):
        if "/" not in value:
            raise SystemExit(
                f"invalid source/target: {label} must be OWNER/REPO, got {value!r}"
            )
        owner, repo_name = value.split("/", 1)
        if not owner or not repo_name:
            raise SystemExit(
                f"invalid source/target: {label} must be OWNER/REPO, got {value!r}"
            )

    source_owner, source_repo = source.split("/", 1)
    target_owner, target_repo = target.split("/", 1)

    codeberg_transport = RequestsTransport()
    github_transport = RequestsTransport()

    codeberg = CodebergClient(
        base_url="https://codeberg.org",
        owner=source_owner,
        repo=source_repo,
        token=codeberg_token,
        transport=codeberg_transport,
    )
    github = GitHubClient(
        base_url="https://api.github.com",
        owner=target_owner,
        repo=target_repo,
        token=github_token,
        transport=github_transport,
    )

    source_url = f"https://codeberg.org/{source}.git"
    target_url = f"https://github.com/{target}.git"
    git = GitMirror(
        source_url=source_url,
        target_url=target_url,
        github_token=github_token,
    )

    state = StateStore(state_path, source, target)
    reporter = Reporter()

    repo = Repository(
        source=source,
        target=target,
        description=getattr(args, "description", None),
        public=bool(getattr(args, "public", False)),
        skip_git=bool(getattr(args, "skip_git", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        yes=bool(getattr(args, "yes", False)),
    )

    orchestrator = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
    )
    return orchestrator


def main() -> None:
    args = parse_args()
    orchestrator = _build_orchestrator(args)
    result = orchestrator.run()
    # Reporter is owned by the orchestrator; render final summary via it.
    reporter = getattr(orchestrator, "reporter", None)
    if reporter is None:
        reporter = getattr(orchestrator, "report", None)
    if reporter is None:
        reporter = Reporter()
    reporter.render_final(result)
    sys.exit(reporter.exit_outcome(result))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Terminating.", file=sys.stderr)
