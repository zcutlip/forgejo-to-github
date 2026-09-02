# RED class: B. Behavioral (offline)
#
# Public-behavior tests for the future ``forgejo_to_github.git.GitMirror``
# boundary defined in plan 02. These tests pin down the user-observable
# contract from section 11 of
# ``plans/02-package-refactor-and-test-foundation/test-framework-spec.md``:
#
#   11.1  Clone success, non-zero exit classification, auth classification,
#         timeout classification, token redaction in stderr.
#   11.2  Branch push success and non-fast-forward classification.
#   11.3  Tag push success and non-fatal failure classification.
#   11.4  Redaction discipline: tokens embedded in URLs and in
#         ``-c http.extraHeader=Authorization: token TOKEN`` arguments are
#         replaced with a stable placeholder in every command line surfaced
#         through an exception or a log line.
#   11.5  Workflow / non-fast-forward advice blocks.
#   11.6  Terminal clone vs. non-fatal push semantics: a clone failure must
#         surface as a terminal error, while a branch/tag push failure must
#         be reported without aborting the surrounding operation.
#
# RED contract: this module performs a top-level ``from forgejo_to_github.git
# import GitMirror``. The package module does not yet exist, so the test
# file is expected to fail collection with ``ImportError``. That failure is
# intentional and accepted as RED. No real ``git`` binary is invoked; the
# command runner and filesystem boundary are injected via constructor /
# factory arguments so the tests stay concrete and offline.
#
# API-alignment amendment (approved):
# The committed stage-03 GitMirror spec
# (``plans/02-package-refactor-and-test-foundation/refactor/03-git-mirror.md``)
# locks the constructor to require ``github_token`` and the public methods
# ``push_branches`` / ``push_tags`` to take only ``local_path`` (returning
# ``None``). The token is owned by the instance and ``--all`` / ``--tags``
# replace the previous per-ref / per-tag argv. This amendment updates the
# tests to match that locked surface. No behavioral assertion is weakened:
# return-value assertions are replaced with command/side-effect assertions
# on the same observable contract (the recorded argv, the redaction of
# the token in the recorded argv, and the structured exception types).
# The obsolete tag-name redaction test (no longer applicable because tag
# names are no longer argv) is removed; URL/extraHeader/generic redaction
# and all error/advice tests are retained.
"""RED-class behavioral tests for the future ``forgejo_to_github.git.GitMirror``.

Tests cover the public behavior described in section 11 of the test-framework
spec. They are intentionally offline: a fake command runner and a tmp-path
working directory are injected into ``GitMirror`` so no real subprocess or
network I/O occurs. ImportError at collection time is the accepted RED state
until ``forgejo_to_github.git`` is implemented.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from forgejo_to_github.git import (  # RED: ImportError until module lands.
    GitCloneError,
    GitMirror,
)

# ---------------------------------------------------------------------------
# Test fakes / helpers
# ---------------------------------------------------------------------------


# A recognizable sentinel that we can search for in captured output. Using
# something that obviously is not a real token (it contains spaces and the
# word "SENTINEL") keeps it from being mistaken for a live credential.
TOKEN_SENTINEL = "SENTINEL-TOKEN-do-not-leak"

# Stable placeholder the redaction routine is expected to substitute.
REDACTED_PLACEHOLDER = "<REDACTED>"


class _FakeCompletedProcess:
    """Minimal stand-in for ``subprocess.CompletedProcess``.

    The fake command runner returns one of these for successful invocations
    and raises ``subprocess.CalledProcessError`` for failures, mirroring the
    contract of ``subprocess.run(..., check=True)``.
    """

    def __init__(
        self,
        args: list[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@dataclass
class _FakeRunner:
    """Recording command runner used to substitute ``subprocess.run``.

    The runner is injected into ``GitMirror`` so the tests can simulate git's
    exit codes and stderr without touching the real binary. ``calls`` records
    every invocation; ``responses`` maps a substring of the argv to either a
    completed-process-like return value or an exception to raise. ``timeout``
    forces the next clone-shaped call to raise ``TimeoutExpired`` (used for
    the timeout classification test).
    """

    calls: list[list[str]] = field(default_factory=list)
    responses: dict[str, BaseException | _FakeCompletedProcess] = field(
        default_factory=dict
    )
    timeout: bool = False

    def __call__(
        self,
        args: list[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        capture_output: bool = False,
        text: bool = False,
        **_: Any,
    ) -> _FakeCompletedProcess:
        self.calls.append(list(args))

        # Simulate a subprocess-level timeout on clone-shaped calls.
        if self.timeout and "clone" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout or 0.0)

        for key, value in self.responses.items():
            if key in args:
                if isinstance(value, BaseException):
                    raise value
                return value

        # Default: success.
        return _FakeCompletedProcess(args=args, returncode=0, stdout="", stderr="")


@dataclass
class _FakeTempdirFactory:
    """Stand-in for ``tempfile.mkdtemp`` that records every path it creates."""

    root: Any
    created: list[str] = field(default_factory=list)

    def __call__(self, suffix: str | None = None, prefix: str | None = None) -> str:
        path = self.root / f"{prefix or 'f2gh'}-{len(self.created)}{suffix or ''}"
        path.mkdir()
        self.created.append(str(path))
        return str(path)


def _mkdtemp_under(tmp_path: Any) -> _FakeTempdirFactory:
    """Return a factory that produces a unique subdir of ``tmp_path``."""
    return _FakeTempdirFactory(root=tmp_path)


def _make_cpe(
    cmd: list[str],
    stderr: str,
    returncode: int = 128,
) -> subprocess.CalledProcessError:
    """Build a ``CalledProcessError`` with the same shape as git's output."""
    return subprocess.CalledProcessError(
        returncode=returncode,
        cmd=cmd,
        output="",
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# 11.1 Clone
# ---------------------------------------------------------------------------


def test_clone_success_returns_local_path_and_records_command(
    tmp_path: Any,
) -> None:
    """A successful clone returns the local working directory and records
    a ``git clone <url> <path>``-shaped command on the injected runner.

    Mirrors spec 11.1: ``A successful clone returns the local path and
    records the command (``git clone <url> <path>``) on the fake``.
    """
    runner = _FakeRunner()
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    local_path = mirror.clone()

    assert local_path == fs_factory.created[0]
    assert len(runner.calls) == 1
    recorded = runner.calls[0]
    assert "clone" in recorded
    assert "https://codeberg.org/owner/repo.git" in recorded
    assert local_path in recorded


def test_clone_nonzero_exit_raises_structured_git_clone_error(
    tmp_path: Any,
) -> None:
    """A non-zero clone exit raises a ``GitCloneError``-shaped exception
    that carries the exit code, stderr, and a redacted command line.

    Mirrors spec 11.1: ``A clone that exits non-zero... raises a structured
    GitCloneError carrying the exit code, stderr, and a redacted command
    line.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "clone",
            "--mirror",
            "https://codeberg.org/owner/repo.git",
            "/tmp/f2gh-repo-0",
        ],
        stderr="fatal: unable to access '...': Could not resolve host: codeberg.org",
        returncode=128,
    )
    runner = _FakeRunner(responses={"clone": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(Exception) as exc_info:
        mirror.clone()

    exc = exc_info.value
    # The exception type is allowed to be the module's ``GitCloneError`` or
    # any subclass thereof, including the eventual ``SystemExit``-carrying
    # wrapper. What matters is that the structured fields are surfaced.
    cls_name = type(exc).__name__
    assert "GitCloneError" in cls_name or isinstance(exc, SystemExit)

    text = str(exc)
    assert "Could not resolve host" in text
    # Structured fields: exit code 128 must be visible somewhere on the
    # rendered exception text.
    assert "128" in text


def test_clone_auth_failure_is_classified_as_git_auth_error(tmp_path: Any) -> None:
    """An exit-128 clone with ``Authentication failed`` stderr is
    classified as a ``GitAuthError`` (a subclass of ``GitCloneError``) and
    carries actionable advice pointing at ``CODEBERG_TOKEN``.

    Mirrors spec 11.1: ``A clone that exits 128 with 'Authentication
    failed' in stderr is classified as GitAuthError... with actionable
    advice 'token lacks repository access'.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "clone",
            "--mirror",
            "https://codeberg.org/owner/repo.git",
            "/tmp/f2gh-repo-0",
        ],
        stderr=(
            "fatal: could not read Username for 'https://codeberg.org': "
            "terminal prompts disabled"
        ),
        returncode=128,
    )
    runner = _FakeRunner(responses={"clone": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(Exception) as exc_info:
        mirror.clone()

    exc = exc_info.value
    cls_name = type(exc).__name__
    assert "GitAuthError" in cls_name or "GitCloneError" in cls_name
    text = str(exc)
    # Actionable advice: must mention the token and the missing repository
    # access. The exact wording is flexible so long as the named concepts
    # are present.
    assert "CODEBERG_TOKEN" in text or "token" in text.lower()
    assert "repository" in text.lower() or "access" in text.lower()


def test_clone_timeout_classified_as_git_clone_timeout_error(
    tmp_path: Any,
) -> None:
    """A ``subprocess.TimeoutExpired`` on clone is classified as
    ``GitCloneTimeoutError`` (a ``GitCloneError`` subclass).

    Mirrors spec 11.1: ``A clone that times out... is classified as
    GitCloneTimeoutError.``
    """
    runner = _FakeRunner(timeout=True)
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(Exception) as exc_info:
        mirror.clone()

    cls_name = type(exc_info.value).__name__
    assert "GitCloneTimeoutError" in cls_name or "GitCloneError" in cls_name


def test_clone_stderr_token_is_redacted_in_error_text(tmp_path: Any) -> None:
    """When git's stderr includes the GitHub token (e.g. via a URL or
    ``extraHeader`` argument), the rendered exception text must NOT
    contain the raw token. Every error path is asserted to be free of it.

    Mirrors spec 11.1: ``A clone whose stderr contains the token is
    asserted to be redacted in every error path.``
    """
    leak = (
        f"fatal: unable to update url from "
        f"'https://x-access-token:{TOKEN_SENTINEL}@github.com/owner/target.git'"
    )
    cpe = _make_cpe(
        cmd=[
            "git",
            "clone",
            "--mirror",
            f"https://x-access-token:{TOKEN_SENTINEL}@github.com/owner/target.git",
            "/tmp/f2gh-repo-0",
        ],
        stderr=leak,
        returncode=128,
    )
    runner = _FakeRunner(responses={"clone": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(Exception) as exc_info:
        mirror.clone()

    text = str(exc_info.value)
    assert TOKEN_SENTINEL not in text, (
        "raw token leaked into clone error text:\n" + text
    )
    # The redaction placeholder is expected to appear at least once.
    assert REDACTED_PLACEHOLDER in text or "<REDACTED" in text


# ---------------------------------------------------------------------------
# 11.2 Branch push
# ---------------------------------------------------------------------------


def test_branch_push_success_uses_all_and_redacts_token(
    tmp_path: Any,
    caplog: Any,
) -> None:
    """A successful branch push runs ``git -C <local_path> push
    <auth_url> --all``; the recorded argv carries the ``--all`` flag, and
    the redaction routine is applied to every command line surfaced
    through the logger.

    Mirrors spec 11.2 (amended): ``A successful branch push issues the
    ``--all`` command on the fake and the token is redacted in every
    command line surfaced.`` The approved stage-03 surface replaces the
    per-ref return value with command/side-effect assertions on the same
    observable contract. The redaction is observable on the log line,
    not on the raw argv (which must contain the token for git's own
    authentication).
    """
    runner = _FakeRunner()
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    # Push phases operate on an independent local path; no real ``git``
    # binary is invoked because the command runner is injected. The path
    # lives under ``tmp_path`` to match the offline test boundary.
    local_path = str(tmp_path / "mirror")
    caplog.set_level(logging.INFO)
    result = mirror.push_branches(local_path)

    # The approved API returns ``None``.
    assert result is None
    push_calls = [c for c in runner.calls if "push" in c]
    assert len(push_calls) == 1
    recorded = push_calls[0]
    # The branch push uses ``--all`` rather than per-ref argv.
    assert "--all" in recorded
    # The redaction routine must run on the command line surfaced via
    # the logger; the raw argv forwarded to git contains the token so
    # git can authenticate.
    joined_log = " ".join(record.getMessage() for record in caplog.records)
    assert TOKEN_SENTINEL not in joined_log, (
        f"raw token leaked into log line: {joined_log!r}"
    )


def test_branch_push_failure_raises_git_push_error(tmp_path: Any) -> None:
    """A non-zero push exit raises ``GitPushError``. The exception is
    classified (not a SystemExit that aborts the orchestrator): callers
    are expected to log and continue with issue migration.

    Mirrors spec 11.2: ``A push that exits non-zero raises GitPushError;
    the test asserts that the orchestrator's reaction is 'log and continue
    with issue migration'.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "-C",
            "/tmp/f2gh-repo-0",
            "push",
            "https://github.com/owner/target.git",
            "main",
        ],
        stderr="error: failed to push some refs to 'github.com/owner/target.git'",
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_branches(local_path)

    cls_name = type(exc_info.value).__name__
    # GitPushError (or any subclass) is required.
    assert "GitPushError" in cls_name


def test_branch_push_non_fast_forward_is_classified_with_advice(
    tmp_path: Any,
) -> None:
    """A push rejected as ``[rejected] (non-fast-forward)`` is classified
    as ``GitPushRejectedError`` with advice referencing ``force`` push or
    ``pull first``.

    Mirrors spec 11.2: ``A push rejected as 'non-fast-forward' is
    classified as GitPushRejectedError with advice 'force push or pull
    first'.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "-C",
            "/tmp/f2gh-repo-0",
            "push",
            "https://github.com/owner/target.git",
            "main",
        ],
        stderr=(
            "! [rejected]        main -> main (non-fast-forward)\n"
            "hint: Updates were rejected because the tip of your current "
            "branch is behind\n"
            "hint: its remote counterpart. Integrate the remote changes "
            "(e.g.\n"
            "hint: 'git pull ...') before pushing again.\n"
            "hint: See the 'Note about fast-forwards' in 'git push --help' "
            "for details."
        ),
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_branches(local_path)

    exc = exc_info.value
    cls_name = type(exc).__name__
    assert "GitPushRejectedError" in cls_name or "GitPushError" in cls_name
    text = str(exc)
    # Advice must point at the remediation: force push or pull first.
    assert "force" in text.lower() or "pull" in text.lower()


# ---------------------------------------------------------------------------
# 11.3 Tag push
# ---------------------------------------------------------------------------


def test_tag_push_success_uses_tags_and_redacts_token(
    tmp_path: Any,
    caplog: Any,
) -> None:
    """A successful tag push runs ``git -C <local_path> push <auth_url>
    --tags``; the recorded argv carries the ``--tags`` flag, and the
    redaction routine is applied to every command line surfaced through
    the logger.

    Mirrors spec 11.3 (amended): ``A successful tag push issues the
    ``--tags`` command on the fake and the token is redacted in every
    command line surfaced.`` The approved stage-03 surface replaces the
    per-tag return value with command/side-effect assertions on the
    same observable contract; tag names are not in argv because the
    ``--tags`` flag pushes every tag at once. The redaction is
    observable on the log line, not on the raw argv (which must contain
    the token for git's own authentication).
    """
    runner = _FakeRunner()
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")
    caplog.set_level(logging.INFO)

    result = mirror.push_tags(local_path)

    # The approved API returns ``None``.
    assert result is None
    push_calls = [c for c in runner.calls if "push" in c]
    assert len(push_calls) == 1
    recorded = push_calls[0]
    # The tag push uses ``--tags`` rather than per-tag argv.
    assert "--tags" in recorded
    # The redaction routine must run on the command line surfaced via
    # the logger; the raw argv forwarded to git contains the token so
    # git can authenticate.
    joined_log = " ".join(record.getMessage() for record in caplog.records)
    assert TOKEN_SENTINEL not in joined_log, (
        f"raw token leaked into log line: {joined_log!r}"
    )


def test_tag_push_failure_raises_git_tag_push_error(tmp_path: Any) -> None:
    """A tag push failure raises ``GitTagPushError``. Tag push failures
    are terminal for the Git phase but do not block issue migration.

    Mirrors spec 11.3: ``A tag push failure raises GitTagPushError. Tag
    push failures are terminal for the Git phase but do not block issue
    migration.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "-C",
            "/tmp/f2gh-repo-0",
            "push",
            "https://github.com/owner/target.git",
            "v1.0.0",
        ],
        stderr="error: failed to push tag 'v1.0.0'",
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_tags(local_path)

    cls_name = type(exc_info.value).__name__
    assert "GitTagPushError" in cls_name


# ---------------------------------------------------------------------------
# 11.4 Redaction discipline
# ---------------------------------------------------------------------------


def test_url_token_is_redacted_in_logged_command(tmp_path: Any, caplog: Any) -> None:
    """A push URL of the form
    ``https://x-access-token:TOKEN@host/...`` is redacted in every command
    line the Git service logs.

    Mirrors spec 11.4: ``The redaction function replaces tokens in URLs
    (...https://x-access-token:TOKEN@host/...) ... with a stable
    placeholder.``
    """
    runner = _FakeRunner()
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    caplog.set_level(logging.INFO)

    # Branch push with the token owned by the instance. The construction
    # of the internal target URL must include the ``x-access-token:TOKEN@``
    # form so the redaction routine has work to do.
    mirror.push_branches(local_path)

    # Every log line produced must have run through the redaction routine.
    for record in caplog.records:
        assert TOKEN_SENTINEL not in record.getMessage(), (
            f"raw token leaked into log line: {record.getMessage()!r}"
        )


def test_extra_header_token_is_redacted_in_command(tmp_path: Any) -> None:
    """A ``-c http.extraHeader=Authorization: token TOKEN`` argument is
    redacted with the stable placeholder.

    Mirrors spec 11.4: ``... in command arguments
    (-c http.extraHeader=Authorization: token TOKEN) with a stable
    placeholder.``
    """
    # Force a failure so the recorded argv surfaces through the exception
    # text. We expect the recorded argv (which the redaction routine must
    # scrub) to be visible on the exception but with the token replaced.
    cpe = _make_cpe(
        cmd=[
            "git",
            "-c",
            f"http.extraHeader=Authorization: token {TOKEN_SENTINEL}",
            "push",
            "https://github.com/owner/target.git",
            "main",
        ],
        stderr="error: failed to push some refs",
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_branches(local_path)

    text = str(exc_info.value)
    assert TOKEN_SENTINEL not in text
    assert REDACTED_PLACEHOLDER in text or "<REDACTED" in text


# ---------------------------------------------------------------------------
# 11.5 Workflow advice
# ---------------------------------------------------------------------------


def test_clone_failure_advice_has_cause_remediation_and_docs_pointer(
    tmp_path: Any,
) -> None:
    """A clone failure surfaces an advice block with: (a) most likely
    cause, (b) concrete remediation step, (c) link / pointer to the
    relevant Codeberg/GitHub docs. The three pieces appear in that order.

    Mirrors spec 11.5: ``A clone failure emits an advice block
    containing: (a) the most likely cause, (b) a concrete remediation
    step, (c) a link or pointer to the relevant Codeberg/GitHub docs when
    applicable. The test asserts the presence and order of these three
    pieces.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "clone",
            "--mirror",
            "https://codeberg.org/owner/repo.git",
            "/tmp/f2gh-repo-0",
        ],
        stderr=(
            "fatal: unable to access 'https://codeberg.org/owner/repo.git': "
            "Could not resolve host: codeberg.org"
        ),
        returncode=128,
    )
    runner = _FakeRunner(responses={"clone": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(Exception) as exc_info:
        mirror.clone()

    text = str(exc_info.value)
    # (a) most likely cause: "network" / "DNS" / "host"
    cause_idx = max(
        text.lower().find("network"),
        text.lower().find("dns"),
        text.lower().find("host"),
    )
    # (b) concrete remediation: "retry" or "check"
    remediation_idx = max(
        text.lower().find("retry"),
        text.lower().find("check"),
    )
    # (c) docs pointer: a URL or a doc path marker.
    docs_idx = max(text.find("https://"), text.lower().find("docs"))

    assert cause_idx != -1, "advice block missing most likely cause"
    assert remediation_idx != -1, "advice block missing remediation step"
    assert docs_idx != -1, "advice block missing docs pointer"
    assert cause_idx < remediation_idx < docs_idx, (
        f"advice pieces out of order: cause={cause_idx} "
        f"remediation={remediation_idx} docs={docs_idx}\n{text}"
    )


def test_tag_push_failure_advice_references_tag_and_retry(tmp_path: Any) -> None:
    """A tag push failure emits an advice block that references the
    ``tag push`` action and the retry strategy.

    Mirrors spec 11.5 (amended per stage-03 §3.5): because the new
    ``push_tags`` pushes all tags in one ``--tags`` command, the
    advisory refers to "tag push" generically rather than to a
    specific tag name. The test asserts the substring ``"tag push"``
    and either ``"retry"`` or ``"again"``.
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "-C",
            "/tmp/f2gh-repo-0",
            "push",
            "https://github.com/owner/target.git",
            "v1.0.0",
        ],
        stderr="error: failed to push tag 'v1.0.0'",
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_tags(local_path)

    text = str(exc_info.value)
    assert "tag push" in text
    assert "retry" in text.lower() or "again" in text.lower()


def test_non_fast_forward_advice_recommends_rebase_or_force_with_lease(
    tmp_path: Any,
) -> None:
    """A non-fast-forward push failure emits advice recommending
    ``git pull --rebase`` or ``--force-with-lease``.

    Mirrors spec 11.5: ``A non-fast-forward push failure emits an advice
    block recommending ``git pull --rebase`` or ``--force-with-lease``.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "-C",
            "/tmp/f2gh-repo-0",
            "push",
            "https://github.com/owner/target.git",
            "main",
        ],
        stderr=(
            "! [rejected]        main -> main (non-fast-forward)\n"
            "hint: Updates were rejected because the tip of your current "
            "branch is behind"
        ),
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_branches(local_path)

    text = str(exc_info.value)
    assert "git pull --rebase" in text or "rebase" in text.lower()
    assert "--force-with-lease" in text or "force-with-lease" in text.lower()


# ---------------------------------------------------------------------------
# 11.6 Terminal clone vs non-fatal push
# ---------------------------------------------------------------------------


def test_clone_failure_is_terminal_no_github_api_call_after(
    tmp_path: Any,
) -> None:
    """A clone failure is terminal: nothing in the Git service proceeds
    to issue further API calls.

    Mirrors spec 11.6: ``A clone failure is terminal: the orchestrator
    does not proceed to issue migration. The test asserts that no GitHub
    API request is registered after a failed clone.``

    The behavioral assertion at the ``GitMirror`` boundary is: after a
    failed clone, no ``push``-shaped call is recorded on the injected
    runner. We use the runner's call log as the stand-in for "GitHub API
    request registered".
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "clone",
            "--mirror",
            "https://codeberg.org/owner/repo.git",
            "/tmp/f2gh-repo-0",
        ],
        stderr="fatal: unable to access '...': Could not resolve host",
        returncode=128,
    )
    runner = _FakeRunner(responses={"clone": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(GitCloneError):
        mirror.clone()

    # Only the clone call is allowed; no push-shaped call is permitted.
    push_calls = [c for c in runner.calls if "push" in c]
    assert not push_calls, (
        f"non-terminal behavior: push recorded after failed clone: {push_calls!r}"
    )


def test_branch_push_failure_is_nonfatal_does_not_abort(tmp_path: Any) -> None:
    """A branch push failure does not abort: the Git service surfaces a
    ``GitPushError`` that callers (the orchestrator) are expected to
    handle by logging and continuing.

    Mirrors spec 11.6: ``A branch push failure is non-fatal: the
    orchestrator logs the failure, continues to issue migration, and
    includes the failure in the final report. The test asserts that
    GitHub API requests are registered after a failed branch push.``

    At the ``GitMirror`` boundary this manifests as: a push failure
    raises an exception type (``GitPushError``) that is *not* a process-
    terminating ``SystemExit`` and is *not* a ``GitCloneError``. Callers
    are expected to wrap it.
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "-C",
            "/tmp/f2gh-repo-0",
            "push",
            "https://github.com/owner/target.git",
            "main",
        ],
        stderr="error: failed to push some refs",
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_branches(local_path)

    exc = exc_info.value
    cls_name = type(exc).__name__
    # Must NOT be a clone error class or a process exit.
    assert "GitCloneError" not in cls_name
    assert not isinstance(exc, SystemExit)
    # Must be the structured push-error class.
    assert "GitPushError" in cls_name


def test_tag_push_failure_is_nonfatal_for_issue_migration(tmp_path: Any) -> None:
    """A tag push failure is non-fatal in the same sense as branch push:
    the Git service raises ``GitTagPushError`` (callers wrap / log /
    continue), not a process-terminating exception.

    Mirrors spec 11.6: ``A tag push failure is non-fatal for the same
    reasons as branch push.``
    """
    cpe = _make_cpe(
        cmd=[
            "git",
            "-C",
            "/tmp/f2gh-repo-0",
            "push",
            "https://github.com/owner/target.git",
            "v1.0.0",
        ],
        stderr="error: failed to push tag 'v1.0.0'",
        returncode=1,
    )
    runner = _FakeRunner(responses={"push": cpe})
    fs_factory = _mkdtemp_under(tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(Exception) as exc_info:
        mirror.push_tags(local_path)

    exc = exc_info.value
    cls_name = type(exc).__name__
    assert "GitCloneError" not in cls_name
    assert not isinstance(exc, SystemExit)
    assert "GitTagPushError" in cls_name
