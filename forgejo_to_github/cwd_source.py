"""Local-checkout source inference and freshness probing (issue #6).

Derives the migration ``owner/repo`` slug from the current working
directory and probes whether the checkout is in sync with its Codeberg
origin. All subprocess work goes through the injected ``runner``
callable (``runner(argv, **kwargs)`` returning an object with
``returncode``/``stdout``/``stderr``); only the remote ``ls-remote``
probe sends the no-prompt SSH environment.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class CwdError(Exception):
    """Raised when the cwd cannot serve as a migration source."""


class ProbeError(Exception):
    """Raised when the remote freshness probe fails."""


@dataclass(frozen=True)
class CwdSource:
    """Validated local-checkout source: slug, absolutized path, origin URL."""

    slug: str
    path: str
    origin_url: str


@dataclass(frozen=True)
class FreshnessResult:
    """Remote-driven staleness comparison result."""

    behind: list[str] = field(default_factory=list)
    divergent: list[str] = field(default_factory=list)
    missing_tags: list[str] = field(default_factory=list)
    moved_tags: list[str] = field(default_factory=list)
    ahead: list[str] = field(default_factory=list)
    local_only_branches: list[str] = field(default_factory=list)
    local_only_tags: list[str] = field(default_factory=list)
    probe_failed: bool = False

    @property
    def requires_prompt(self) -> bool:
        """True iff behind/divergent/missing/moved refs exist."""
        return bool(
            self.behind or self.divergent or self.missing_tags or self.moved_tags
        )


def _strip_git_suffix(path: str) -> str:
    """Strip one trailing ``.git`` suffix when present."""
    if path.endswith(".git"):
        return path[: -len(".git")]
    return path


def _split_owner_repo(path: str) -> tuple[str, str] | None:
    """Split a URL/scp path into ``(owner, repo)`` or return None."""
    cleaned = path.strip().strip("/")
    cleaned = _strip_git_suffix(cleaned).strip("/")
    parts = cleaned.split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts
    if not owner or not repo:
        return None
    return (owner, repo)


def parse_codeberg_slug(url: str) -> tuple[str, str] | None:
    """Parse a Codeberg remote URL into ``(owner, repo)``.

    Accepts HTTPS/SSH/scp forms with an optional ``.git`` suffix or
    trailing slash; the host match is case-insensitive while
    owner/repo case is preserved. Returns None otherwise.
    """
    if not url or not url.strip():
        return None
    text = url.strip()

    # scp-like form: [user@]host:path (no scheme). Must contain "@"
    # before the colon so Host aliases and insteadOf shorthands
    # (e.g. ``cb:o/r``) are rejected.
    if "://" not in text and "@" in text and ":" in text:
        at_split = text.split("@", 1)
        if len(at_split) != 2:
            return None
        host_and_path = at_split[1]
        host, sep, path = host_and_path.partition(":")
        if not sep or not host or not path:
            return None
        if host.lower() != "codeberg.org":
            return None
        return _split_owner_repo(path.lstrip("/"))

    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host != "codeberg.org":
        return None
    return _split_owner_repo(parsed.path or "")


def resolve_origin_url(runner: Callable[..., Any]) -> str | None:
    """Resolve ``origin`` via ``git ls-remote --get-url origin``.

    Returns the stripped stdout, or None on empty output/failure.
    """
    try:
        result = runner(["git", "ls-remote", "--get-url", "origin"])
    except Exception:  # noqa: BLE001 — any runner failure means "no origin"
        return None
    try:
        if getattr(result, "returncode", 1) != 0:
            return None
        stdout = str(getattr(result, "stdout", "") or "")
    except Exception:  # noqa: BLE001 — malformed result means "no origin"
        return None
    stripped = stdout.strip()
    return stripped if stripped else None


def infer_cwd_source(runner: Callable[..., Any]) -> CwdSource:
    """Validate the cwd as a Codeberg checkout and return its source.

    Checks in order: work tree, origin, Codeberg match, not-shallow,
    not-partial; the first failure raises :class:`CwdError`.
    """
    try:
        work_tree = runner(["git", "rev-parse", "--is-inside-work-tree"])
        work_ok = getattr(work_tree, "returncode", 1) == 0 and str(
            getattr(work_tree, "stdout", "") or ""
        ).strip().lower() == "true"
    except Exception:  # noqa: BLE001 — runner failure means "not a work tree"
        work_ok = False
    if not work_ok:
        raise CwdError("cwd is not inside a git work tree")

    origin_url = resolve_origin_url(runner)
    if origin_url is None:
        raise CwdError("cwd has no git origin")

    parsed = parse_codeberg_slug(origin_url)
    if parsed is None:
        raise CwdError(f"origin '{origin_url}' is not a Codeberg remote")
    owner, repo = parsed

    shallow = runner(["git", "rev-parse", "--is-shallow-repository"])
    if str(getattr(shallow, "stdout", "") or "").strip().lower() == "true":
        raise CwdError("cwd is a shallow checkout; re-clone without --depth")

    partial = runner(["git", "config", "--get", "extensions.partialclone"])
    if getattr(partial, "returncode", 1) == 0 and str(
        getattr(partial, "stdout", "") or ""
    ).strip():
        raise CwdError("cwd is a partial clone; re-clone without --filter")

    return CwdSource(
        slug=f"{owner}/{repo}",
        path=str(Path.cwd().resolve()),
        origin_url=origin_url,
    )


def remote_ref_tips(
    runner: Callable[..., Any], url: str
) -> dict[str, str]:
    """Map remote ``ls-remote`` tips to ``{ref: sha}`` (heads+tags only).

    Sends the no-prompt SSH environment; drops synthetic namespaces
    and peeled ``^{}`` lines; skips malformed lines. Nonzero return
    raises :class:`ProbeError`.
    """
    env: dict[str, str] = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    try:
        result = runner(["git", "ls-remote", url], env=env)
    except Exception as exc:
        raise ProbeError(f"git ls-remote failed for {url}: {exc}") from exc
    if getattr(result, "returncode", 1) != 0:
        stderr = str(getattr(result, "stderr", "") or "").strip()
        detail = f": {stderr}" if stderr else ""
        raise ProbeError(f"git ls-remote failed for {url}{detail}")
    stdout = str(getattr(result, "stdout", "") or "")
    tips: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if "\t" not in line:
            continue
        sha, _, ref = line.partition("\t")
        sha = sha.strip()
        ref = ref.strip()
        if not sha or not ref:
            continue
        if ref.endswith("^{}"):
            continue
        if ref.startswith(("refs/heads/", "refs/tags/")):
            tips[ref] = sha
    return tips


def local_ref_map(runner: Callable[..., Any]) -> dict[str, str]:
    """Parse ``git show-ref`` output into a ``{ref: sha}`` map.

    Returncode 1 with empty stdout means an empty map.
    """
    result = runner(["git", "show-ref"])
    stdout = str(getattr(result, "stdout", "") or "")
    if getattr(result, "returncode", 0) != 0 and not stdout.strip():
        return {}
    refs: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        sha, ref = parts
        if not sha or not ref:
            continue
        refs[ref.strip()] = sha.strip()
    return refs


def ref_contains(
    runner: Callable[..., Any], ancestor_sha: str, ref: str
) -> bool:
    """Report whether ``ancestor_sha`` is an ancestor of ``ref``."""
    result = runner(["git", "merge-base", "--is-ancestor", ancestor_sha, ref])
    return getattr(result, "returncode", 1) == 0


def _short_branch(ref: str) -> str:
    """Strip the ``refs/heads/`` prefix from a branch ref."""
    return ref[len("refs/heads/") :]


def _short_tag(ref: str) -> str:
    """Strip the ``refs/tags/`` prefix from a tag ref."""
    return ref[len("refs/tags/") :]


def check_freshness(
    runner: Callable[..., Any], origin_url: str
) -> FreshnessResult:
    """Compare remote tips against local refs (remote-driven).

    Catches :class:`ProbeError` and returns a ``probe_failed``
    result instead of raising for probe failure.
    """
    try:
        remote = remote_ref_tips(runner, origin_url)
    except ProbeError:
        return FreshnessResult(probe_failed=True)
    local = local_ref_map(runner)

    remote_heads = {
        ref: sha for ref, sha in remote.items() if ref.startswith("refs/heads/")
    }
    remote_tags = {
        ref: sha for ref, sha in remote.items() if ref.startswith("refs/tags/")
    }
    local_heads = {
        ref: sha for ref, sha in local.items() if ref.startswith("refs/heads/")
    }
    local_tags = {
        ref: sha for ref, sha in local.items() if ref.startswith("refs/tags/")
    }
    local_shas = set(local.values())

    behind: list[str] = []
    divergent: list[str] = []
    missing_tags: list[str] = []
    moved_tags: list[str] = []
    ahead: list[str] = []

    for ref in sorted(remote_heads):
        remote_sha = remote_heads[ref]
        short = _short_branch(ref)
        local_sha = local_heads.get(ref)
        if local_sha is None:
            behind.append(short)
        elif local_sha == remote_sha:
            continue
        elif ref_contains(runner, remote_sha, local_sha):
            ahead.append(short)
        elif remote_sha in local_shas:
            divergent.append(short)
        else:
            behind.append(short)

    for ref in sorted(remote_tags):
        remote_sha = remote_tags[ref]
        short = _short_tag(ref)
        local_sha = local_tags.get(ref)
        if local_sha is None:
            missing_tags.append(short)
        elif local_sha != remote_sha:
            moved_tags.append(short)

    local_only_branches = sorted(
        _short_branch(ref) for ref in local_heads if ref not in remote_heads
    )
    local_only_tags = sorted(
        _short_tag(ref) for ref in local_tags if ref not in remote_tags
    )

    return FreshnessResult(
        behind=behind,
        divergent=divergent,
        missing_tags=missing_tags,
        moved_tags=moved_tags,
        ahead=ahead,
        local_only_branches=local_only_branches,
        local_only_tags=local_only_tags,
    )


def format_freshness_prompt(result: FreshnessResult) -> str | None:
    """Build the unified stale-state consent prompt, or None when clean."""
    if not result.requires_prompt:
        return None
    parts: list[str] = []
    if result.behind:
        parts.append(f"branches behind: {', '.join(result.behind)}")
    for branch in result.divergent:
        parts.append(f"branch {branch} not containing origin's tip")
    if result.missing_tags:
        parts.append(f"tags missing locally: {', '.join(result.missing_tags)}")
    if result.moved_tags:
        parts.append(f"tags moved upstream: {', '.join(result.moved_tags)}")
    base = ", ".join(parts)
    local_bits: list[str] = []
    if result.local_only_branches:
        local_bits.append(
            f"local-only branches [{', '.join(result.local_only_branches)}]"
            " will migrate"
        )
    if result.local_only_tags:
        local_bits.append(
            f"local-only tags [{', '.join(result.local_only_tags)}] will migrate"
        )
    if local_bits:
        base = f"{base}; {', '.join(local_bits)}"
    return f"{base} — migrate local state anyway?"


def format_local_only_notice(result: FreshnessResult) -> str | None:
    """Build the announce-only notice for local-only refs, or None."""
    if not result.local_only_branches and not result.local_only_tags:
        return None
    bits: list[str] = []
    if result.local_only_branches:
        bits.append(
            f"branches [{', '.join(result.local_only_branches)}]"
        )
    if result.local_only_tags:
        bits.append(f"tags [{', '.join(result.local_only_tags)}]")
    return f"local-only refs that will migrate: {', '.join(bits)}"


def has_uncommitted_changes(runner: Callable[..., Any]) -> bool:
    """Report whether ``git status --porcelain`` shows uncommitted work."""
    result = runner(["git", "status", "--porcelain"])
    stdout = str(getattr(result, "stdout", "") or "")
    return bool(stdout.strip())
