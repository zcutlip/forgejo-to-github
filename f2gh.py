#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from forgejo_to_github import __version__
from forgejo_to_github.about import about
from forgejo_to_github.codeberg import CodebergClient
from forgejo_to_github.domain import Repository
from forgejo_to_github.git import GitMirror
from forgejo_to_github.github import GitHubClient
from forgejo_to_github.migration import MigrationOrchestrator
from forgejo_to_github.paths import (
    cache_path_for,
    default_cache_base,
    default_state_base,
    state_path_for,
)
from forgejo_to_github.reporting import Reporter
from forgejo_to_github.state import (
    StateLockedError,
    StateStore,
    StateWriteError,
)
from forgejo_to_github.transport import RequestsTransport

# SIGINT convention: 128 + signal number (SIGINT = 2).
EXIT_INTERRUPTED = 130

# State-path refusal (preflight could not establish the state file).
EXIT_STATE_ERROR = 3


def parse_args() -> argparse.Namespace:
    description = about()
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    parser.add_argument(
        "--state-file",
        metavar="PATH",
        default=None,
        help="Path to the migration state file (default: per-migration location under the platform user-state directory)",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove this migration's cached git mirror and exit (no migration run)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="Show version and exit",
    )
    return parser.parse_args()


def _make_prompter(repo: Repository) -> Callable[[str, bool], bool]:
    """Construct a prompter callable for the orchestrator's pre-flight phase.

    When ``repo.yes`` is True, the prompter auto-affirms without reading
    stdin. Otherwise it prompts via ``input()`` with a ``[y/N]`` suffix
    and returns True for "y" or "yes" (case-insensitive), False otherwise.
    """

    def prompter(prompt: str, default: bool = False) -> bool:
        if repo.yes:
            return True
        try:
            answer = input(f"{prompt} [y/N] ")
        except EOFError:
            return default
        return answer.strip().lower() in ("y", "yes")

    return prompter


def _split_owner_repo(label: str, value: str) -> tuple[str, str]:
    """Split an ``OWNER/REPO`` identity, exiting on malformed input.

    ``label`` names the offending flag (``source``/``target``) in the
    error message, matching the CLI's historical wording.
    """
    if "/" not in value:
        raise SystemExit(
            f"invalid source/target: {label} must be OWNER/REPO, got {value!r}"
        )
    owner, repo_name = value.split("/", 1)
    if not owner or not repo_name:
        raise SystemExit(
            f"invalid source/target: {label} must be OWNER/REPO, got {value!r}"
        )
    return owner, repo_name


def _resolve_state_path(args: argparse.Namespace) -> Path:
    """Resolve the migration state file path for this run.

    An explicit ``--state-file`` is used verbatim. Otherwise the state
    file lives under the platform user-state directory, namespaced per
    source→target migration so each migration keeps its own checkpoint.
    ``source``/``target`` are validated before a path is derived.
    """
    explicit = getattr(args, "state_file", None)
    if explicit:
        return Path(explicit)
    source = str(getattr(args, "source", ""))
    target = str(getattr(args, "target", ""))
    _split_owner_repo("source", source)
    _split_owner_repo("target", target)
    return state_path_for(default_state_base(), source, target)


def _resolve_mirror_path(args: argparse.Namespace) -> Path:
    """Resolve the cached git mirror path for this run.

    The mirror lives under the platform user-cache directory,
    namespaced per source→target migration — the same layout helper
    as the state path, rooted at the cache base instead. ``source`` /
    ``target`` are validated before a path is derived.
    """
    source = str(getattr(args, "source", ""))
    target = str(getattr(args, "target", ""))
    _split_owner_repo("source", source)
    _split_owner_repo("target", target)
    return cache_path_for(default_cache_base(), source, target)


def _run_clean(args: argparse.Namespace, state_path: Path) -> int:
    """Remove this migration's cached mirror and exit without migrating.

    Needs no tokens and performs no network I/O: the state path only
    supplies the run lock, so ``--clean`` refuses (via the existing
    lock handler) while another run holds this migration. Under
    ``--dry-run`` the mirror is reported but never deleted.
    """
    source = str(getattr(args, "source", ""))
    target = str(getattr(args, "target", ""))
    mirror_path = _resolve_mirror_path(args)
    if bool(getattr(args, "dry_run", False)):
        print(f"Would remove cached mirror: {mirror_path}")
        return 0
    store = StateStore(state_path, source, target)
    # Non-blocking acquire through the existing machinery: raises
    # StateLockedError while another run holds this migration (handled
    # by main's lock handler) and StateWriteError when the path is
    # unusable (handled by main's state-error handler).
    store.prepare()
    try:
        if mirror_path.is_dir() and not mirror_path.is_symlink():
            shutil.rmtree(mirror_path)
            print(f"Removed cached mirror: {mirror_path}")
        elif mirror_path.is_symlink() or mirror_path.is_file():
            mirror_path.unlink()
            print(f"Removed cached mirror: {mirror_path}")
        else:
            print(f"No cached mirror for {source} -> {target}: {mirror_path}")
        return 0
    finally:
        store.release()


def _build_orchestrator(args: argparse.Namespace) -> MigrationOrchestrator:
    """Construct the production :class:`MigrationOrchestrator`.

    Performs all collaborator construction that :func:`main` delegates
    to. Reads tokens from the environment (with ``gh auth token``
    fallback for GitHub), validates ``source``/``target`` shape,
    and builds the five collaborators plus the :class:`Repository`
    value object.
    """
    # State file path: explicit --state-file, else the per-migration default.
    state_path = _resolve_state_path(args)

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
    source_owner, source_repo = _split_owner_repo("source", source)
    target_owner, target_repo = _split_owner_repo("target", target)

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
        mirror_path=str(_resolve_mirror_path(args)),
    )

    orchestrator = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
        prompter=_make_prompter(repo),
    )
    return orchestrator


def main() -> None:
    args = parse_args()
    state_path = _resolve_state_path(args)
    try:
        if bool(getattr(args, "clean", False)):
            sys.exit(_run_clean(args, state_path))
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
    except KeyboardInterrupt:
        if args.dry_run:
            print("Interrupted by user.", file=sys.stderr)
        else:
            print(
                f"Interrupted by user — state saved to {state_path}, "
                f"resume with f2gh --source {args.source} --target {args.target}",
                file=sys.stderr,
            )
        sys.exit(EXIT_INTERRUPTED)
    except StateLockedError as exc:
        print(
            "Another f2gh run is already migrating this pair.",
            file=sys.stderr,
        )
        print(f"  State path: {exc.state_path}", file=sys.stderr)
        print(
            "  Wait for it to finish, or remove the lock file once it has stopped.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE_ERROR)
    except StateWriteError as exc:
        print(
            "Could not write the migration state; stopping.",
            file=sys.stderr,
        )
        print(f"  State path: {exc.path}", file=sys.stderr)
        print(f"  Reason: {exc.reason}", file=sys.stderr)
        print(
            "  The most recent work may not be recorded — check the target "
            "repository before re-running.",
            file=sys.stderr,
        )
        sys.exit(EXIT_STATE_ERROR)


if __name__ == "__main__":
    main()
