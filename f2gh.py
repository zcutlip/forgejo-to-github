#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from forgejo_to_github import __version__
from forgejo_to_github.about import about
from forgejo_to_github.codeberg import CodebergClient
from forgejo_to_github.cwd_source import (
    CwdError,
    CwdSource,
    FreshnessResult,
    check_freshness,
    format_freshness_prompt,
    format_local_only_notice,
    has_uncommitted_changes,
    infer_cwd_source,
)
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
from forgejo_to_github.reporting import EXIT_DECLINED, Reporter
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

# User-declined prompt — single source of truth is reporting.EXIT_DECLINED
# (imported above); listed here so every exit code stays visible in one block.


def parse_args() -> argparse.Namespace:
    description = about()
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        required=False,
        default=None,
        metavar="OWNER/REPO",
        help="Source repo on Codeberg (e.g. 'myuser/myproject')",
    )
    parser.add_argument(
        "--target",
        required=False,
        default=None,
        metavar="OWNER/REPO",
        help="Target repo on GitHub (e.g. 'myuser/myproject')",
    )
    parser.add_argument(
        "--cwd",
        action="store_true",
        help="Infer the source from the current checkout without a confirmation prompt",
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


def _make_yes_prompter(assume_yes: bool) -> Callable[[str, bool], bool]:
    """Construct an ``input()`` prompter honouring a pre-resolved ``--yes`` flag.

    When ``assume_yes`` is True the prompter auto-affirms without reading
    stdin. Otherwise it prompts via ``input()`` with a ``[y/N]`` suffix
    and returns True for "y" or "yes" (case-insensitive), False otherwise
    (including ``EOFError`` on a non-tty, which maps to the default deny).
    """

    def prompter(prompt: str, default: bool = False) -> bool:
        if assume_yes:
            return True
        try:
            answer = input(f"{prompt} [y/N] ")
        except EOFError:
            return default
        return answer.strip().lower() in ("y", "yes")

    return prompter


def _make_prompter(repo: Repository) -> Callable[[str, bool], bool]:
    """Construct a prompter callable for the orchestrator's pre-flight phase.

    When ``repo.yes`` is True, the prompter auto-affirms without reading
    stdin. Otherwise it prompts via ``input()`` with a ``[y/N]`` suffix
    and returns True for "y" or "yes" (case-insensitive), False otherwise.
    """
    return _make_yes_prompter(bool(repo.yes))


def _split_owner_repo(label: str, value: str) -> tuple[str, str]:
    """Split an ``OWNER/REPO`` identity, exiting on malformed input.

    ``label`` names the offending flag (``source``/``target``) in the
    error message, matching the CLI's historical wording. Malformed
    input is a usage error (exit 2).
    """
    owner, sep, repo_name = value.partition("/")
    if not sep or not owner or not repo_name:
        print(
            f"invalid source/target: {label} must be OWNER/REPO, got {value!r}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return owner, repo_name


def _git_runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Production git runner for cwd inference and freshness probes.

    Matches the ``runner(argv, **kwargs)`` seam the ``cwd_source``
    helpers expect. Probe-supplied ``env`` is merged over the process
    environment (rather than replacing it) so ``git`` stays resolvable.
    """
    env = kwargs.pop("env", None)
    if env is not None:
        merged = dict(os.environ)
        merged.update(env)
        kwargs["env"] = merged
    return subprocess.run(argv, capture_output=True, text=True, check=False, **kwargs)


def _infer_cwd_source(runner: Callable[..., Any]) -> CwdSource:
    """Infer the source from the current checkout, mapping failure to exit 2.

    A checkout that fails validation (not a work tree, no origin,
    non-Codeberg origin, shallow, partial) is a usage error: the issues
    API needs a Codeberg slug, so the run stops before anything mutates.
    """
    try:
        return infer_cwd_source(runner)
    except CwdError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2)


def resolve_source(
    args: argparse.Namespace,
    runner: Callable[..., Any],
    prompter: Callable[..., bool],
) -> tuple[str, CwdSource | None]:
    """Resolve the migration source slug, inferring from the cwd when omitted.

    An explicit ``--source`` is shape-validated and passes through with
    no cwd probing and no prompt. An omitted source is inferred from the
    current checkout behind a single confirm prompt naming the slug and
    path (declining aborts with exit 5); ``--cwd`` expresses that intent
    up front and skips the prompt. ``--cwd`` combined with an explicit
    source must agree with the inference (mismatch is a usage error,
    exit 2). ``--yes`` cannot confirm an inference that never happens,
    so ``--yes`` with neither an explicit source nor ``--cwd`` is a
    usage error raised before any probing.
    """
    explicit = getattr(args, "source", None)
    use_cwd = bool(getattr(args, "cwd", False))
    assume_yes = bool(getattr(args, "yes", False))
    if explicit is not None:
        _split_owner_repo("source", explicit)
        if not use_cwd:
            return explicit, None
        inferred = _infer_cwd_source(runner)
        if inferred.slug != explicit:
            print(
                f"error: --cwd infers source {inferred.slug!r} from the "
                f"current checkout, which does not match --source {explicit!r}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return explicit, inferred
    if assume_yes and not use_cwd:
        print(
            "error: --yes requires an explicit --source (or --cwd to infer it)",
            file=sys.stderr,
        )
        raise SystemExit(2)
    inferred = _infer_cwd_source(runner)
    if use_cwd:
        return inferred.slug, inferred
    prompt = f"Inferred source {inferred.slug} from {inferred.path} — proceed?"
    if not prompter(prompt):
        print("Aborted.", file=sys.stderr)
        raise SystemExit(EXIT_DECLINED)
    return inferred.slug, inferred


def resolve_target(
    args: argparse.Namespace,
    source_slug: str,
    user_resolver: Callable[[], str | None],
) -> str:
    """Resolve the migration target slug, defaulting to the token's user.

    An explicit ``--target`` is shape-validated and passes through with
    no login lookup. An omitted target defaults to
    ``<gh-user>/<source-repo>`` where ``<gh-user>`` comes from
    ``user_resolver`` (``GET /user`` against the resolved token) and the
    repo from the resolved source slug; a lookup yielding ``None`` or
    raising requires an explicit ``--target`` (exit 2). ``--yes`` cannot
    confirm a default that needs a lookup, so ``--yes`` with an omitted
    target is a usage error raised without calling the resolver.
    """
    explicit = getattr(args, "target", None)
    if explicit is not None:
        _split_owner_repo("target", explicit)
        return explicit
    if bool(getattr(args, "yes", False)):
        print(
            "error: --yes requires an explicit --target", file=sys.stderr
        )
        raise SystemExit(2)
    try:
        login = user_resolver()
    except Exception as exc:  # noqa: BLE001 — any lookup failure requires explicit --target
        print(
            "error: could not determine the GitHub user "
            f"({exc}); pass --target explicitly",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if not login:
        print(
            "error: could not determine the GitHub user; "
            "pass --target explicitly",
            file=sys.stderr,
        )
        raise SystemExit(2)
    _, repo_name = _split_owner_repo("source", source_slug)
    return f"{login}/{repo_name}"


def git_source_url(source_slug: str, cwd: CwdSource | None) -> str:
    """Select the clone source URL for this run.

    A validated local checkout clones from its absolutized path verbatim
    (plain path form, never a ``file://`` URL, so same-filesystem
    hardlinks apply); otherwise the clone goes over the network from
    Codeberg HTTPS.
    """
    if cwd is not None:
        return cwd.path
    return f"https://codeberg.org/{source_slug}.git"


def gate_local_source(
    runner: Callable[..., Any],
    cwd: CwdSource,
    prompter: Callable[..., bool],
) -> FreshnessResult:
    """Announce the local clone source and gate on staleness before mutating.

    Prints the ``Using local checkout ...`` line first, then the dirt
    notice when uncommitted changes are present (informational only —
    uncommitted work is unreachable from refs so it cannot affect the
    migration). A failed freshness probe warns on stderr and proceeds
    with no prompt. Otherwise a stale result (behind, divergent,
    missing, or moved refs) prompts once via the prompter seam
    (declining aborts with exit 5); a fresh result only announces
    local-only refs that will migrate. Returns the freshness result.
    """
    print(
        f"Using local checkout {cwd.path} as clone source (origin {cwd.origin_url})"
    )
    if has_uncommitted_changes(runner):
        print("uncommitted changes present; only branches and tags migrate")
    freshness = check_freshness(runner, cwd.origin_url)
    if freshness.probe_failed:
        print(
            "warning: could not verify local checkout freshness; "
            "proceeding with local state",
            file=sys.stderr,
        )
        return freshness
    if freshness.requires_prompt:
        prompt = format_freshness_prompt(freshness)
        if prompt is None:
            prompt = "migrate local state anyway?"
        if not prompter(prompt):
            print("Aborted.", file=sys.stderr)
            raise SystemExit(EXIT_DECLINED)
        return freshness
    notice = format_local_only_notice(freshness)
    if notice is not None:
        print(notice)
    return freshness


def resolve_clean_target(args: argparse.Namespace) -> str:
    """Resolve the ``--clean`` target, which is always explicit.

    Deriving a default would need a tokened ``GET /user`` lookup,
    contradicting ``--clean``'s tokenless, offline guarantee — so a
    missing ``--target`` is a usage error (exit 2) in plain, ``--cwd``,
    and ``--dry-run`` forms alike. Pure: no token reads, no network.
    """
    target = getattr(args, "target", None)
    if not isinstance(target, str) or not target:
        print(
            "error: --clean requires an explicit --target", file=sys.stderr
        )
        raise SystemExit(2)
    return target


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


def _resolve_github_token() -> str:
    """Resolve the GitHub token: env var first, then ``gh auth token`` fallback."""
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
    return github_token


def _reject_user_lookup() -> str | None:
    """User resolver that must never fire (explicit ``--target`` short-circuits)."""
    raise AssertionError("user lookup must not run with an explicit --target")


def _resolve_migration_target(args: argparse.Namespace, source_slug: str) -> str:
    """Resolve the migration target, inferring from ``GET /user`` when omitted.

    The lookup client is built with the source owner/repo, the resolved
    token, and the standard transport, so the default target names the
    account the credential actually authenticates as. Only constructed
    when ``--target`` is omitted — explicit targets never touch tokens
    or the network here.
    """
    if getattr(args, "target", None) is not None:
        return resolve_target(args, source_slug, _reject_user_lookup)
    github_token = _resolve_github_token()
    source_owner, source_repo = _split_owner_repo("source", source_slug)
    github_user_client = GitHubClient(
        base_url="https://api.github.com",
        owner=source_owner,
        repo=source_repo,
        token=github_token,
        transport=RequestsTransport(),
    )
    return resolve_target(
        args, source_slug, github_user_client.get_current_user
    )


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
    github_token = _resolve_github_token()

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

    source_url = git_source_url(source, getattr(args, "cwd_source", None))
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
    state_path: Path | None = None
    try:
        # Resolve source/target before anything else: explicit slugs pass
        # through untouched, while omitted ones infer (cwd checkout for the
        # source, GET /user for the target) with the documented prompts.
        # The resolved slugs are stored back so state/mirror paths, the
        # orchestrator, and the resume hint all observe the same values.
        prompter = _make_yes_prompter(bool(getattr(args, "yes", False)))
        source_slug, cwd_source = resolve_source(args, _git_runner, prompter)
        args.source = source_slug
        args.cwd_source = cwd_source
        if bool(getattr(args, "clean", False)):
            # --clean stays tokenless and offline: source may infer from the
            # checkout (local probes only), but the target must be explicit.
            args.target = resolve_clean_target(args)
        else:
            args.target = _resolve_migration_target(args, source_slug)
            if cwd_source is not None and not bool(getattr(args, "skip_git", False)):
                gate_local_source(_git_runner, cwd_source, prompter)
        state_path = _resolve_state_path(args)
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
        if args.dry_run or state_path is None:
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
