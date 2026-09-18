"""Cwd-independent, per-migration on-disk paths.

State and cache locations are derived from the platform's user
directories (via ``platformdirs``), never from the process working
directory, so a run does not depend on where it was launched from.
"""

from __future__ import annotations

from pathlib import Path

import platformdirs

#: Application name used for the platform user-state and user-cache roots.
APP_NAME: str = "f2gh"


def default_state_base() -> Path:
    """Return the platform user-state root for this application."""
    return Path(platformdirs.user_state_dir(APP_NAME))


def default_cache_base() -> Path:
    """Return the platform user-cache root for this application."""
    return Path(platformdirs.user_cache_dir(APP_NAME))


def state_path_for(base: Path, source: str, target: str) -> Path:
    """Return the per-migration state file path under ``base``.

    ``source`` and ``target`` are ``OWNER/REPO`` strings. The layout is
    ``<base>/<source-owner>/<source-repo>/<target-owner>/<target-repo>/state.json``
    so each source→target migration keeps an independent checkpoint.
    """
    source_owner, source_repo = source.split("/", 1)
    target_owner, target_repo = target.split("/", 1)
    return (
        Path(base)
        / source_owner
        / source_repo
        / target_owner
        / target_repo
        / "state.json"
    )


def cache_path_for(base: Path, source: str, target: str) -> Path:
    """Return the per-migration cached-mirror path under ``base``.

    ``source`` and ``target`` are ``OWNER/REPO`` strings. The layout is
    ``<base>/<source-owner>/<source-repo>/<target-owner>/<target-repo>/mirror.git``
    so each source→target migration keeps an independent clone cache.
    """
    source_owner, source_repo = source.split("/", 1)
    target_owner, target_repo = target.split("/", 1)
    return (
        Path(base)
        / source_owner
        / source_repo
        / target_owner
        / target_repo
        / "mirror.git"
    )
