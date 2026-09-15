"""State file location helpers for per-migration state paths.

The migration state file is laid out per source/target pair under a base
directory, so concurrent or repeated migrations for different repositories do
not share a single ``state.json`` in the process working directory. The
default base directory is platform-appropriate user state storage.
"""

from __future__ import annotations

from pathlib import Path

import platformdirs
import pytest
from forgejo_to_github.paths import default_state_base, state_path_for


def test_state_path_for_namespaces_by_source_and_target(tmp_path: Path) -> None:
    """State path nests source owner/repo and target owner/repo under the base."""
    assert state_path_for(
        tmp_path, "codeberg-owner/widgets", "github-owner/widgets"
    ) == (
        tmp_path
        / "codeberg-owner"
        / "widgets"
        / "github-owner"
        / "widgets"
        / "state.json"
    )


def test_default_state_base_uses_platform_user_state_dir() -> None:
    """Default base is the platform user state directory for f2gh."""
    assert default_state_base() == Path(platformdirs.user_state_dir("f2gh"))


def test_state_path_for_is_stable_for_the_same_pair(tmp_path: Path) -> None:
    """Identical inputs produce the same state path on repeated calls."""
    first = state_path_for(tmp_path, "codeberg-owner/widgets", "github-owner/widgets")
    second = state_path_for(tmp_path, "codeberg-owner/widgets", "github-owner/widgets")

    assert first == second


def test_state_path_for_differs_for_different_pairs(tmp_path: Path) -> None:
    """Different source/target pairs map to different state paths."""
    path_a = state_path_for(tmp_path, "o/a", "g/a")
    path_b = state_path_for(tmp_path, "o/b", "g/b")

    assert path_a != path_b


def test_default_state_base_does_not_depend_on_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default base is unchanged when the process working directory changes."""
    before = default_state_base()

    monkeypatch.chdir(tmp_path)

    assert default_state_base() == before
