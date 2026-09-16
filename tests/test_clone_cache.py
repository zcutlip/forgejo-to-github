"""Clone-cache contract for issue #5.

Retain a successful ``git clone --mirror`` in a persistent per-migration
cache so a later run can retry the push without re-cloning.

Contract points under test (``plans/04-retain-clone-cache.md``):

* ``GitMirror.clone_into(path)`` clones the mirror into the given
  caller-owned path (no tempdir seam); a partial path is removed when the
  clone fails, times out, or is interrupted.
* ``GitMirror.cached_mirror_is_valid(path)`` reports whether a cached
  mirror is reusable: the directory exists, it is a bare repository
  (``git rev-parse --is-bare-repository``), and its
  ``remote.origin.url`` matches the live source URL. Any validation
  failure means "invalid" (safe direction: fall back to a fresh clone).
* The orchestrator checkpoints ``clone_path`` through the state seam
  immediately after a successful clone.
* Resume with a valid cache skips the clone and retries the push.
* Resume with an invalid cache removes it via the injected ``cleanup``
  seam (scoped to the resolved cache path) before re-cloning.
* A push failure keeps the cache for retry (no cleanup); a state seam
  without ``mirror_path`` falls back to the derived platform default.
* ``Repository`` carries the resolved cache location as optional
  ``mirror_path`` (default None).

Each test pins its own contract point: a missing method or field, or a
concrete assertion on recorded calls (concrete collaborators with mocked
I/O throughout).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from forgejo_to_github.git import GitCloneError, GitMirror
from forgejo_to_github.migration import MigrationOrchestrator

SOURCE_URL = "https://codeberg.org/owner/source.git"
TARGET_URL = "https://github.com/owner/target.git"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_cpe(stderr: str, returncode: int = 128) -> subprocess.CalledProcessError:
    """Build a CalledProcessError shaped like git's failure output."""
    return subprocess.CalledProcessError(
        returncode=returncode,
        cmd=["git"],
        output="",
        stderr=stderr,
    )


@dataclass
class _ScriptedRunner:
    """Command runner routing an argv substring to stdout text or an error."""

    routes: dict[str, str | BaseException] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(
        self,
        args: list[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
        timeout: float | None = None,
        **_: Any,
    ) -> Any:
        self.calls.append(list(args))
        joined = " ".join(args)
        for key, value in self.routes.items():
            if key in joined:
                if isinstance(value, BaseException):
                    raise value
                return SimpleNamespace(
                    args=args, returncode=0, stdout=value, stderr=""
                )
        return SimpleNamespace(args=args, returncode=0, stdout="", stderr="")


def _make_mirror(runner: _ScriptedRunner) -> GitMirror:
    return GitMirror(
        source_url=SOURCE_URL,
        target_url=TARGET_URL,
        github_token="not-a-real-token",
        command_runner=runner,
        cleanup=lambda path: None,
    )


class _CacheFakeGit:
    """New-contract git seam: explicit cache path, validity probe, cleanup."""

    def __init__(self, *, valid: bool, fail_push: bool = False) -> None:
        self.valid = valid
        self.fail_push = fail_push
        self.validity_checks: list[str] = []
        self.clone_into_calls: list[str] = []
        self.push_calls: list[tuple[str, str]] = []
        self.cleanup_calls: list[str] = []

    def cached_mirror_is_valid(self, path: str) -> bool:
        self.validity_checks.append(path)
        return self.valid

    def clone_into(self, path: str) -> str:
        self.clone_into_calls.append(path)
        return path

    def push_branches(self, local_path: str) -> None:
        self.push_calls.append(("branches", local_path))
        if self.fail_push:
            raise RuntimeError("branch push failed")

    def push_tags(self, local_path: str) -> None:
        self.push_calls.append(("tags", local_path))
        if self.fail_push:
            raise RuntimeError("tag push failed")

    def cleanup(self, local_path: str) -> None:
        self.cleanup_calls.append(local_path)


class _CacheFakeState:
    """Concrete-shaped state seam recording save() payloads."""

    def __init__(self, loaded: dict[str, Any]) -> None:
        self._loaded = dict(loaded)
        self.saves: list[dict[str, Any]] = []

    def load(self) -> dict[str, Any]:
        return dict(self._loaded)

    def save(
        self,
        repo_created: bool,
        git_pushed: bool,
        migrated: dict[int, int],
        clone_path: str | None = None,
    ) -> None:
        self.saves.append(
            {
                "repo_created": repo_created,
                "git_pushed": git_pushed,
                "migrated": dict(migrated),
                "clone_path": clone_path,
            }
        )


class _EmptyCodeberg:
    def list_issues(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []


class _ExistingTargetGitHub:
    def check_repository_exists(self) -> dict[str, Any]:
        return {"exists": True, "open_issues_count": 0}


def _fresh_loaded(clone_path: Any = None) -> dict[str, Any]:
    return {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": False,
        "git_pushed": False,
        "migrated": {},
        "clone_path": clone_path,
    }


def _make_repo(mirror_path: Any) -> Any:
    return SimpleNamespace(
        source="owner/source",
        target="owner/target",
        mirror_path=mirror_path,
        dry_run=False,
        skip_git=False,
        yes=True,
    )


def _run_orchestrator(
    *, git: Any, state: Any, mirror_path: Any
) -> tuple[Any, Any]:
    orch = MigrationOrchestrator(
        repo=_make_repo(mirror_path),
        codeberg=_EmptyCodeberg(),
        github=_ExistingTargetGitHub(),
        git=git,
        state=state,
        reporter=None,
    )
    return orch.run(), orch


# ---------------------------------------------------------------------------
# clone_into
# ---------------------------------------------------------------------------


def test_clone_into_clones_mirror_into_given_path(tmp_path: Any) -> None:
    """clone_into runs ``git clone --mirror <src> <path>`` and returns it."""
    runner = _ScriptedRunner()
    mirror = _make_mirror(runner)
    cache_path = str(tmp_path / "mirror")

    returned = mirror.clone_into(cache_path)

    assert returned == cache_path
    assert runner.calls == [["git", "clone", "--mirror", SOURCE_URL, cache_path]]


def test_clone_into_failure_removes_partial_path(tmp_path: Any) -> None:
    """A failed clone_into removes the partial path and raises GitCloneError."""
    runner = _ScriptedRunner(
        routes={"clone": _make_cpe("fatal: Could not resolve host: codeberg.org")}
    )
    mirror = _make_mirror(runner)
    cache_path = tmp_path / "mirror"
    cache_path.mkdir()
    (cache_path / "partial").write_text("orphan")

    with pytest.raises(GitCloneError):
        mirror.clone_into(str(cache_path))

    assert not cache_path.exists()


def test_clone_into_keyboard_interrupt_removes_partial_path(tmp_path: Any) -> None:
    """An interrupted clone_into re-raises and removes the partial path.

    KeyboardInterrupt is caught and converted only in the test harness
    sense: the interrupt must propagate (else branch fails), and the
    path must be gone. Never let the interrupt escape the test function.
    """
    runner = _ScriptedRunner(routes={"clone": KeyboardInterrupt()})
    mirror = _make_mirror(runner)
    cache_path = tmp_path / "mirror"
    cache_path.mkdir()

    try:
        mirror.clone_into(str(cache_path))
    except KeyboardInterrupt:
        pass
    else:
        pytest.fail("expected KeyboardInterrupt to propagate")

    assert not cache_path.exists()


# ---------------------------------------------------------------------------
# cached_mirror_is_valid
# ---------------------------------------------------------------------------


def test_cached_mirror_is_valid_for_matching_bare_mirror(tmp_path: Any) -> None:
    """Existing dir + bare + matching origin URL validates True."""
    runner = _ScriptedRunner(
        routes={"rev-parse": "true\n", "origin": SOURCE_URL + "\n"}
    )
    mirror = _make_mirror(runner)
    cache_path = tmp_path / "mirror"
    cache_path.mkdir()

    assert mirror.cached_mirror_is_valid(str(cache_path)) is True


def test_cached_mirror_is_invalid_when_path_missing(tmp_path: Any) -> None:
    """A missing path is invalid without spawning any subprocess."""
    runner = _ScriptedRunner()
    mirror = _make_mirror(runner)

    assert mirror.cached_mirror_is_valid(str(tmp_path / "mirror")) is False
    assert runner.calls == []


def test_cached_mirror_is_invalid_when_not_bare(tmp_path: Any) -> None:
    """A non-bare repository is invalid."""
    runner = _ScriptedRunner(
        routes={"rev-parse": "false\n", "origin": SOURCE_URL + "\n"}
    )
    mirror = _make_mirror(runner)
    cache_path = tmp_path / "mirror"
    cache_path.mkdir()

    assert mirror.cached_mirror_is_valid(str(cache_path)) is False


def test_cached_mirror_is_invalid_when_origin_mismatches(tmp_path: Any) -> None:
    """A mirror of a different source is invalid (safe direction)."""
    runner = _ScriptedRunner(
        routes={
            "rev-parse": "true\n",
            "origin": "https://codeberg.org/owner/other.git\n",
        }
    )
    mirror = _make_mirror(runner)
    cache_path = tmp_path / "mirror"
    cache_path.mkdir()

    assert mirror.cached_mirror_is_valid(str(cache_path)) is False


def test_cached_mirror_is_invalid_when_validation_command_fails(
    tmp_path: Any,
) -> None:
    """A failing validation command means invalid, never a hard error."""
    runner = _ScriptedRunner(
        routes={"rev-parse": _make_cpe("fatal: not a git repository")}
    )
    mirror = _make_mirror(runner)
    cache_path = tmp_path / "mirror"
    cache_path.mkdir()

    assert mirror.cached_mirror_is_valid(str(cache_path)) is False


# ---------------------------------------------------------------------------
# Orchestrator: checkpoint, resume, retry
# ---------------------------------------------------------------------------


def test_git_phase_checkpoints_clone_path_after_clone(tmp_path: Any) -> None:
    """The producer-driven value: post-clone state carries the clone path."""
    cache_path = str(tmp_path / "mirror")
    git = _CacheFakeGit(valid=False)
    state = _CacheFakeState(_fresh_loaded())

    _run_orchestrator(git=git, state=state, mirror_path=cache_path)

    assert git.validity_checks == [cache_path]
    assert git.clone_into_calls == [cache_path]
    assert any(
        save.get("clone_path") == cache_path for save in state.saves
    ), f"no save checkpointed clone_path={cache_path!r}: {state.saves!r}"


def test_resume_with_valid_cache_skips_clone(tmp_path: Any) -> None:
    """A valid cached mirror is pushed from without re-cloning.

    Concrete collaborators: a real ``GitMirror`` with a scripted command
    runner and a real ``StateStore`` pre-seeded (via raw JSON) with the
    ``clone_path`` checkpoint.
    """
    from forgejo_to_github.state import StateStore

    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    cache_path = str(mirror_dir)
    runner = _ScriptedRunner(
        routes={"rev-parse": "true\n", "origin": SOURCE_URL + "\n"}
    )
    cleanups: list[str] = []

    def _record_cleanup(path: str, *args: Any, **kwargs: Any) -> None:
        cleanups.append(path)

    git = GitMirror(
        source_url=SOURCE_URL,
        target_url=TARGET_URL,
        github_token="not-a-real-token",
        command_runner=runner,
        cleanup=_record_cleanup,
    )
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "source": "owner/source",
                "target": "owner/target",
                "repo_created": False,
                "git_pushed": False,
                "migrated": {},
                "clone_path": cache_path,
            }
        )
    )
    state = StateStore(state_path, "owner/source", "owner/target")

    result, _ = _run_orchestrator(git=git, state=state, mirror_path=cache_path)

    assert [argv for argv in runner.calls if "clone" in argv] == []
    push_argvs = [argv for argv in runner.calls if "push" in argv]
    assert any("--all" in argv for argv in push_argvs)
    assert any("--tags" in argv for argv in push_argvs)
    assert cleanups == []
    assert result.failures == []


def test_resume_with_invalid_cache_removes_it_then_reclones(
    tmp_path: Any,
) -> None:
    """An invalid cache is removed via cleanup before the fresh clone."""
    cache_path = str(tmp_path / "mirror")
    git = _CacheFakeGit(valid=False)
    state = _CacheFakeState(_fresh_loaded(clone_path=cache_path))

    _run_orchestrator(git=git, state=state, mirror_path=cache_path)

    assert git.cleanup_calls == [cache_path]
    assert git.clone_into_calls == [cache_path]


def test_push_failure_keeps_cache_for_retry(tmp_path: Any) -> None:
    """A failed push keeps the cache (no cleanup) with clone_path saved."""
    cache_path = str(tmp_path / "mirror")
    git = _CacheFakeGit(valid=False, fail_push=True)
    state = _CacheFakeState(_fresh_loaded())

    result, _ = _run_orchestrator(git=git, state=state, mirror_path=cache_path)

    assert result.git["push"] == "failed"
    assert git.cleanup_calls == []
    assert any(
        save.get("clone_path") == cache_path and save.get("git_pushed") is False
        for save in state.saves
    ), f"expected unpushed clone_path checkpoint: {state.saves!r}"


def test_git_phase_derives_default_cache_path_when_repo_has_none(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an explicit path the run still clones under the user cache.

    Locks the plan clarification: a ``Repository`` without ``mirror_path``
    (non-CLI construction) derives the platform default instead of
    crashing, so existing constructions keep working.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    git = _CacheFakeGit(valid=False)
    state = _CacheFakeState(_fresh_loaded())
    repo = SimpleNamespace(
        source="owner/source",
        target="owner/target",
        dry_run=False,
        skip_git=False,
        yes=True,
    )
    orch = MigrationOrchestrator(
        repo=repo,
        codeberg=_EmptyCodeberg(),
        github=_ExistingTargetGitHub(),
        git=git,
        state=state,
        reporter=None,
    )

    orch.run()

    assert len(git.clone_into_calls) == 1
    assert git.clone_into_calls[0].startswith(str(tmp_path)), (
        f"derived cache path escaped the user cache: {git.clone_into_calls!r}"
    )


# ---------------------------------------------------------------------------
# Repository carries the resolved cache location
# ---------------------------------------------------------------------------


def test_repository_accepts_optional_mirror_path() -> None:
    """Repository takes an optional ``mirror_path`` defaulting to None."""
    from forgejo_to_github.domain import Repository

    assert (
        Repository(
            source="owner/source",
            target="owner/target",
            mirror_path="some/cache/mirror.git",
        ).mirror_path
        == "some/cache/mirror.git"
    )
    assert (
        Repository(source="owner/source", target="owner/target").mirror_path
        is None
    )
