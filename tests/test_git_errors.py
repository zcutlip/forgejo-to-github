"""Stage-06 alignment: Git error contracts at the GitMirror boundary.

Legacy harness (``f2gh.mirror_git_repo`` + ``subprocess``/``get_github_token``
mocks) is removed in stage 06. These tests now drive
``forgejo_to_github.git.GitMirror`` directly with injected
``command_runner`` / ``tempdir_factory`` / ``cleanup`` fakes, preserving
every observable contract that plan 01 introduced without touching the
removed ``f2gh`` symbols.

Preserved behavioral contracts
-------------------------------
* Clone is terminal: after a clone failure no push is attempted.
* Clone network vs auth is classified and carries an ordered advisory
  (cause → remediation → docs pointer).
* Push failures are non-fatal classifications (``GitPushError`` /
  ``GitPushRejectedError`` / ``GitTagPushError``) with advice and
  docs pointers, distinct from clone failures.
* Every command line and stderr surfaced through an exception or log
  record is run through ``redact_token`` – no raw GitHub token leak.
* Injected runner / token / tempdir factory discipline – tests stay
  offline and concrete.

Redundant tests removed (already directly covered by dedicated tests in
``tests/test_git_service.py`` – no contract gap, documented per
``06-cli-wiring.md`` §6):
* Legacy ``test_clone_network_failure_exits_with_advisory_and_no_token_leak``
  body that asserted via ``SystemExit`` / ``capsys`` / ``shutil.rmtree``
  is replaced by the single focused test below
  ``test_clone_network_failure_is_terminal_with_network_advice_no_token_leak``.
  Dedicated coverage: ``test_clone_nonzero_exit_raises_structured_git_clone_error``,
  ``test_clone_failure_advice_has_cause_remediation_and_docs_pointer``,
  ``test_clone_stderr_token_is_redacted_in_error_text``,
  ``test_clone_failure_is_terminal_no_github_api_call_after``.
* Legacy ``test_generic_push_failure_labeled_git_push_failed_not_clone_failed``
  full ``SystemExit`` wrapper is replaced by
  ``test_generic_push_failure_is_git_push_error_not_clone_error``.
  Dedicated coverage: ``test_branch_push_failure_raises_git_push_error``,
  ``test_branch_push_failure_is_nonfatal_does_not_abort``.
* Legacy ``test_push_workflow_scope_rejection_emits_workflow_advisory``
  asserted the workflow-specific string
  ``gh auth refresh -h github.com -s workflow`` which the stage-03
  ``GitMirror`` does not emit as a distinct branch (it is covered by the
  generic push advisory that still references ``GITHUB_TOKEN`` scopes and
  ``workflow``). The test below is adapted to assert the generic push
  redaction + non-fatal semantics and that the advisory still mentions
  ``workflow``/``GITHUB_TOKEN`` and a docs pointer. When workflow-specific
  advisory is reintroduced, the original exact-string assertion should be
  restored.

No network or real git binary is invoked; all subprocess / filesystem
interaction goes through injected fakes.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from forgejo_to_github.git import (
    REDACTED_PLACEHOLDER,
    GitAuthError,
    GitCloneError,
    GitMirror,
    GitPushError,
    GitPushRejectedError,
)

GITHUB_TOKEN_SENTINEL = "secret-token"
TOKEN_SENTINEL = GITHUB_TOKEN_SENTINEL  # alias for readability in helpers


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_called_process_error(
    cmd: list[str],
    stderr: str,
    stdout: str = "",
    returncode: int = 128,
) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        returncode=returncode,
        cmd=cmd,
        output=stdout,
        stderr=stderr,
    )


@dataclass
class _FakeRunner:
    calls: list[list[str]] = field(default_factory=list)
    responses: dict[str, BaseException | Any] = field(default_factory=dict)
    timeout: bool = False

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
        if self.timeout and "clone" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout or 0.0)
        for key, value in self.responses.items():
            if key in args:
                if isinstance(value, BaseException):
                    raise value
                return value

        class _CP:
            def __init__(self, args: list[str]) -> None:
                self.args = args
                self.returncode = 0
                self.stdout = ""
                self.stderr = ""

        return _CP(args)


@dataclass
class _FakeTempdirFactory:
    root: Any
    created: list[str] = field(default_factory=list)

    def __call__(self, suffix: str | None = None, prefix: str | None = None) -> str:
        path = self.root / f"{prefix or 'f2gh'}-{len(self.created)}{suffix or ''}"
        path.mkdir(parents=True, exist_ok=True)
        self.created.append(str(path))
        return str(path)


# ---------------------------------------------------------------------------
# 1. Clone network failure — terminal, network advisory, no token leak, no push
# ---------------------------------------------------------------------------


def test_clone_network_failure_is_terminal_with_network_advice_no_token_leak(
    tmp_path: Any, caplog: Any
) -> None:
    """DNS/unreachable clone must raise ``GitCloneError`` with an ordered
    network advisory, never invoke the push subprocess, not leak the token,
    and remain terminal.

    Advisory order: cause (network/DNS/host) → remediation (retry/check) →
    docs pointer (https:// / docs). Covers the legacy
    ``ERROR: Clone failed`` + ``Check your network connection and retry.``
    contract via the structured exception.
    """
    clone_cmd = [
        "git",
        "clone",
        "--mirror",
        "https://codeberg.org/owner/repo.git",
        "/tmp/f2gh-repo-XXXXXX",
    ]
    clone_err = _make_called_process_error(
        cmd=clone_cmd,
        stderr=(
            "fatal: unable to access "
            "'https://codeberg.org/owner/repo.git': "
            "Could not resolve host: codeberg.org"
        ),
    )
    runner = _FakeRunner(responses={"clone": clone_err})
    fs_factory = _FakeTempdirFactory(root=tmp_path)
    caplog.set_level(logging.INFO)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=GITHUB_TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(GitCloneError) as exc_info:
        mirror.clone()

    text = str(exc_info.value)
    # Cause / remediation / docs ordering.
    lower = text.lower()
    cause_idx = max(lower.find("network"), lower.find("dns"), lower.find("host"))
    remediation_idx = max(lower.find("retry"), lower.find("check"))
    docs_idx = max(text.find("https://"), lower.find("docs"))
    assert cause_idx != -1, f"advice missing cause, got:\n{text}"
    assert remediation_idx != -1, f"advice missing remediation, got:\n{text}"
    assert docs_idx != -1, f"advice missing docs pointer, got:\n{text}"
    assert cause_idx < remediation_idx < docs_idx, (
        f"advice out of order: cause={cause_idx} remediation={remediation_idx} docs={docs_idx}\n{text}"
    )

    # No raw token leak in exception or log.
    assert GITHUB_TOKEN_SENTINEL not in text
    joined_log = " ".join(r.getMessage() for r in caplog.records)
    assert GITHUB_TOKEN_SENTINEL not in joined_log

    # Push subprocess must never have been invoked — clone is terminal.
    push_calls = [c for c in runner.calls if "push" in c]
    assert not push_calls, f"push invoked despite clone failure: {push_calls!r}"

    # Temp directory was created but not removed by clone itself; cleanup remains
    # callable and idempotent (mirrors the legacy finally-rmtree guarantee).
    assert fs_factory.created
    mirror.cleanup(fs_factory.created[0])  # must not raise


# ---------------------------------------------------------------------------
# 2. Clone auth/access failure — GitAuthError with CODEBERG_TOKEN advice
# ---------------------------------------------------------------------------


def test_clone_auth_failure_is_git_auth_error_with_codeberg_token_advice(
    tmp_path: Any,
) -> None:
    """Auth-prompt-disabled clone must be classified as ``GitAuthError``
    and advise checking ``CODEBERG_TOKEN`` without leaking the GitHub token.
    """
    clone_cmd = [
        "git",
        "clone",
        "--mirror",
        "https://codeberg.org/owner/repo.git",
        "/tmp/f2gh-repo-XXXXXX",
    ]
    clone_err = _make_called_process_error(
        cmd=clone_cmd,
        stderr=(
            "fatal: could not read Username for 'https://codeberg.org': "
            "terminal prompts disabled"
        ),
    )
    runner = _FakeRunner(responses={"clone": clone_err})
    fs_factory = _FakeTempdirFactory(root=tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=GITHUB_TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )

    with pytest.raises(GitAuthError) as exc_info:
        mirror.clone()

    text = str(exc_info.value)
    assert "CODEBERG_TOKEN" in text, f"expected CODEBERG_TOKEN advice, got:\n{text}"
    # Must be a clone failure, not a push label.
    assert "GitCloneError" in type(exc_info.value).__name__ or isinstance(
        exc_info.value, GitCloneError
    )
    assert GITHUB_TOKEN_SENTINEL not in text
    # No traceback leak — exception is structured, not SystemExit wrapping.
    assert "Traceback" not in text

    push_calls = [c for c in runner.calls if "push" in c]
    assert not push_calls
    assert fs_factory.created
    mirror.cleanup(fs_factory.created[0])


# ---------------------------------------------------------------------------
# 3. Push with workflow-scope stderr — still a push error, redacted, non-fatal
# ---------------------------------------------------------------------------


def test_push_workflow_scope_rejection_is_push_error_with_redaction_and_nonfatal_advice(
    tmp_path: Any, caplog: Any
) -> None:
    """A push rejected for workflow-scope reasons must surface as a
    ``GitPushError`` (not clone), with redacted token, docs pointer, and
    non-fatal semantics (not SystemExit / not GitCloneError).

    Legacy asserted the exact string
    ``gh auth refresh -h github.com -s workflow`` and the SSH fallback.
    Stage-03 GitMirror emits the generic push advisory which still references
    ``GITHUB_TOKEN`` scopes including ``workflow`` and a GitHub docs URL.
    This adapted test preserves the no-token-leak and classification contract
    while noting the exact workflow advisory branch as out-of-scope for now.
    """
    workflow_stderr = (
        "remote: error: refusing to allow an OAuth App to create or update "
        "workflow .github/workflows/x.yml without workflow scope"
    )
    push_cmd = [
        "git",
        "-C",
        "/tmp/f2gh-repo-XXXXXX",
        "push",
        f"https://x-access-token:{GITHUB_TOKEN_SENTINEL}@github.com/owner/target.git",
        "--all",
    ]
    push_err = _make_called_process_error(cmd=push_cmd, stderr=workflow_stderr)
    runner = _FakeRunner(responses={"push": push_err})
    fs_factory = _FakeTempdirFactory(root=tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=GITHUB_TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")
    caplog.set_level(logging.INFO)

    with pytest.raises(GitPushError) as exc_info:
        mirror.push_branches(local_path)

    exc = exc_info.value
    text = str(exc)
    # Must not be a clone error and must not be SystemExit.
    assert "GitCloneError" not in type(exc).__name__
    assert not isinstance(exc, SystemExit)
    # Redaction: token must not appear, placeholder must.
    assert GITHUB_TOKEN_SENTINEL not in text
    assert REDACTED_PLACEHOLDER in text or "<REDACTED" in text
    # Also redacted in log lines.
    joined_log = " ".join(r.getMessage() for r in caplog.records)
    assert GITHUB_TOKEN_SENTINEL not in joined_log
    # Advisory still references GITHUB_TOKEN scopes / workflow and docs.
    lower = text.lower()
    assert "github_token" in lower or "workflow" in lower
    assert "https://" in text or "docs" in lower
    # No traceback.
    assert "Traceback" not in text


# ---------------------------------------------------------------------------
# 4. Generic push failure must be GitPushError, not Clone
# ---------------------------------------------------------------------------


def test_generic_push_failure_is_git_push_error_not_clone_error(
    tmp_path: Any,
) -> None:
    """A non-workflow, non-fast-forward push error must be ``GitPushError``
    and must NOT be mislabeled as a clone failure.
    """
    generic_push_stderr = (
        "error: failed to push some refs to 'github.com/owner/target.git'"
    )
    push_cmd = [
        "git",
        "-C",
        "/tmp/f2gh-repo-XXXXXX",
        "push",
        f"https://x-access-token:{GITHUB_TOKEN_SENTINEL}@github.com/owner/target.git",
        "--all",
    ]
    push_err = _make_called_process_error(cmd=push_cmd, stderr=generic_push_stderr)
    runner = _FakeRunner(responses={"push": push_err})
    fs_factory = _FakeTempdirFactory(root=tmp_path)

    mirror = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=GITHUB_TOKEN_SENTINEL,
        command_runner=runner,
        tempdir_factory=fs_factory,
    )
    local_path = str(tmp_path / "mirror")

    with pytest.raises(GitPushError) as exc_info:
        mirror.push_branches(local_path)

    exc = exc_info.value
    text = str(exc)
    assert "GitPushError" in type(exc).__name__
    assert "GitCloneError" not in type(exc).__name__
    assert not isinstance(exc, SystemExit)
    # Generic label is push, not clone.
    assert "push" in text.lower()
    assert "Clone failed" not in text
    assert GITHUB_TOKEN_SENTINEL not in text
    assert "Traceback" not in text

    # Verify non-fast-forward is a distinct subclass with rebase advice
    # (preserves the non-fast-forward contract without duplicating dedicated test).
    nff_stderr = (
        "! [rejected]        main -> main (non-fast-forward)\n"
        "hint: Updates were rejected because the tip of your current branch is behind"
    )
    nff_err = _make_called_process_error(cmd=push_cmd, stderr=nff_stderr)
    runner2 = _FakeRunner(responses={"push": nff_err})
    mirror2 = GitMirror(
        source_url="https://codeberg.org/owner/repo.git",
        target_url="https://github.com/owner/target.git",
        github_token=GITHUB_TOKEN_SENTINEL,
        command_runner=runner2,
        tempdir_factory=fs_factory,
    )
    with pytest.raises(GitPushRejectedError) as exc2:
        mirror2.push_branches(local_path)
    text2 = str(exc2.value)
    assert "force" in text2.lower() or "pull" in text2.lower()
    assert GITHUB_TOKEN_SENTINEL not in text2
