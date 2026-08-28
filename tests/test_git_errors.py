"""RED-stage regression tests for ``mirror_git_repo`` failure paths.

These tests pin down the user-observable contracts that plan 01 introduced:

1. A network-level clone failure (e.g. ``Could not resolve host``) must terminate
   with ``SystemExit`` carrying a message that names ``ERROR: Clone failed``,
   recommends ``Check your network connection and retry.``, contains no raw
   token, never invokes the push subprocess, and still cleans up the temporary
   directory.
2. A clone auth/access failure (e.g. ``could not read Username``) must terminate
   cleanly and point the user at ``CODEBERG_TOKEN``.
3. A workflow-scope push rejection must produce the GitHub workflow advisory
   containing ``gh auth refresh -h github.com -s workflow`` and
   ``git remote add github git@github.com:owner/target.git``, with no raw
   token leaked.
4. A generic push failure must be labeled ``Git push failed``, not
   ``Clone failed``.

These are regression tests. They should fail RED only if current behavior
violates them. No network access is performed; ``subprocess.run`` and
``get_github_token`` are replaced with mocks.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

import f2gh

GITHUB_TOKEN_SENTINEL = "secret-token"


def _make_called_process_error(
    cmd: list[str],
    stderr: str,
    stdout: str = "",
    returncode: int = 128,
) -> subprocess.CalledProcessError:
    """Build a ``CalledProcessError`` shaped like git's real output."""
    return subprocess.CalledProcessError(
        returncode=returncode,
        cmd=cmd,
        output=stdout,
        stderr=stderr,
    )


def _patch_token(monkeypatch: pytest.MonkeyPatch) -> str:
    """Force ``get_github_token`` to return the sentinel value."""
    monkeypatch.setattr(f2gh, "get_github_token", lambda: GITHUB_TOKEN_SENTINEL)
    return GITHUB_TOKEN_SENTINEL


# ---------------------------------------------------------------------------
# 1. Clone network failure
# ---------------------------------------------------------------------------


def test_clone_network_failure_exits_with_advisory_and_no_token_leak(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """DNS/unreachable clone must SystemExit, advertise the network advisory,
    never reach the push subprocess, and still clean up the temp directory.

    The token used to authenticate the GitHub push URL must not leak into any
    captured output, because it ends up embedded in ``target_url`` and would
    otherwise be visible inside an exception's rendered ``stderr``/``cmd``.
    """
    _patch_token(monkeypatch)

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

    # Track subprocess.run invocations so we can verify push is never called.
    run_calls: list[list[str]] = []

    def fake_run(args, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        run_calls.append(list(args))
        # Only the clone command is expected to fire; raise on push to fail loudly.
        if "clone" in args:
            raise clone_err
        raise AssertionError(
            f"subprocess.run was called with an unexpected command: {args!r}"
        )

    cleanup_calls: list[str] = []

    def fake_rmtree(path, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        cleanup_calls.append(str(path))

    monkeypatch.setattr(f2gh.subprocess, "run", fake_run)
    monkeypatch.setattr(f2gh.shutil, "rmtree", fake_rmtree)

    with pytest.raises(SystemExit) as exc_info:
        f2gh.mirror_git_repo(
            source="owner/repo",
            target="owner/target",
            dry_run=False,
        )

    # SystemExit code must be nonzero (the test contract just says "causes
    # SystemExit"; we verify the code is the message itself, mirroring existing
    # mirror_git_repo behavior).
    code = exc_info.value.code
    assert code is not None
    assert code != 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    # Clone-failure header must be present.
    assert "ERROR: Clone failed" in combined, (
        "expected 'ERROR: Clone failed' in output, got:\n" + combined
    )

    # Network advice must be present.
    assert "Check your network connection and retry." in combined, (
        "expected the network advisory in output, got:\n" + combined
    )

    # Raw GitHub token must NOT leak anywhere in the captured output.
    assert GITHUB_TOKEN_SENTINEL not in combined, (
        "raw GitHub token leaked into output:\n" + combined
    )

    # The push subprocess must never have been invoked.
    push_calls = [c for c in run_calls if "push" in c]
    assert not push_calls, (
        f"push subprocess was invoked despite clone failure: {push_calls!r}"
    )

    # Temp directory cleanup must have been attempted.
    assert cleanup_calls, "expected shutil.rmtree to be called for tmpdir cleanup"


# ---------------------------------------------------------------------------
# 2. Clone auth/access failure
# ---------------------------------------------------------------------------


def test_clone_auth_failure_mentions_codeberg_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When git cannot read a username (auth prompt disabled), the clone
    error path must produce a clean ``SystemExit`` and recommend checking
    ``CODEBERG_TOKEN``. No raw GitHub token should leak.
    """
    _patch_token(monkeypatch)

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

    run_calls: list[list[str]] = []

    def fake_run(args, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        run_calls.append(list(args))
        if "clone" in args:
            raise clone_err
        raise AssertionError(
            f"subprocess.run was called with an unexpected command: {args!r}"
        )

    cleanup_calls: list[str] = []

    def fake_rmtree(path, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        cleanup_calls.append(str(path))

    monkeypatch.setattr(f2gh.subprocess, "run", fake_run)
    monkeypatch.setattr(f2gh.shutil, "rmtree", fake_rmtree)

    with pytest.raises(SystemExit) as exc_info:
        f2gh.mirror_git_repo(
            source="owner/repo",
            target="owner/target",
            dry_run=False,
        )

    code = exc_info.value.code
    assert code is not None
    assert code != 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    # The auth-failure advice must mention CODEBERG_TOKEN.
    assert "CODEBERG_TOKEN" in combined, (
        "expected 'CODEBERG_TOKEN' advice in output, got:\n" + combined
    )

    # The message must still be a clone failure (not a generic push failure).
    assert "ERROR: Clone failed" in combined, (
        "expected 'ERROR: Clone failed' in output, got:\n" + combined
    )

    # Raw GitHub token must NOT leak.
    assert GITHUB_TOKEN_SENTINEL not in combined, (
        "raw GitHub token leaked into output:\n" + combined
    )

    # No traceback leaked to stderr.
    assert "Traceback" not in combined, (
        "expected no traceback in output, got:\n" + combined
    )

    # Push must not have been called.
    push_calls = [c for c in run_calls if "push" in c]
    assert not push_calls, (
        f"push subprocess was invoked despite clone auth failure: {push_calls!r}"
    )

    # Cleanup must still occur.
    assert cleanup_calls, "expected shutil.rmtree to be called for tmpdir cleanup"


# ---------------------------------------------------------------------------
# 3. Push workflow-scope rejection
# ---------------------------------------------------------------------------


def test_push_workflow_scope_rejection_emits_workflow_advisory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A push rejected for workflow-scope reasons must produce the
    workflow advisory that references ``gh auth refresh`` and the
    ``git remote add github`` SSH fallback. No raw GitHub token may leak.
    """
    _patch_token(monkeypatch)

    workflow_stderr = (
        "remote: error: refusing to allow an OAuth App to create or update "
        "workflow .github/workflows/x.yml without workflow scope"
    )
    push_cmd = [
        "git",
        "-C",
        "/tmp/f2gh-repo-XXXXXX",
        "push",
        # Note: token intentionally embedded so the redaction path is exercised.
        f"https://x-access-token:{GITHUB_TOKEN_SENTINEL}@github.com/owner/target.git",
        "--all",
    ]
    push_err = _make_called_process_error(cmd=push_cmd, stderr=workflow_stderr)

    run_calls: list[list[str]] = []

    def fake_run(args, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        run_calls.append(list(args))
        if "clone" in args:
            # Clone succeeds.
            return MagicMock(returncode=0, stdout="", stderr="")
        if "push" in args:
            raise push_err
        raise AssertionError(
            f"subprocess.run was called with an unexpected command: {args!r}"
        )

    cleanup_calls: list[str] = []

    def fake_rmtree(path, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        cleanup_calls.append(str(path))

    monkeypatch.setattr(f2gh.subprocess, "run", fake_run)
    monkeypatch.setattr(f2gh.shutil, "rmtree", fake_rmtree)

    with pytest.raises(SystemExit) as exc_info:
        f2gh.mirror_git_repo(
            source="owner/repo",
            target="owner/target",
            dry_run=False,
        )

    code = exc_info.value.code
    assert code is not None
    assert code != 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    # The workflow advisory must mention the refresh command.
    assert "gh auth refresh -h github.com -s workflow" in combined, (
        "expected 'gh auth refresh -h github.com -s workflow' in advisory, got:\n"
        + combined
    )

    # The workflow advisory must mention the SSH remote-add fallback.
    assert "git remote add github git@github.com:owner/target.git" in combined, (
        "expected 'git remote add github git@github.com:owner/target.git' in "
        f"advisory, got:\n{combined}"
    )

    # Raw GitHub token must NOT leak anywhere.
    assert GITHUB_TOKEN_SENTINEL not in combined, (
        "raw GitHub token leaked into workflow advisory output:\n" + combined
    )

    # No traceback leaked to stderr.
    assert "Traceback" not in combined, (
        "expected no traceback in output, got:\n" + combined
    )

    # Cleanup must still occur.
    assert cleanup_calls, "expected shutil.rmtree to be called for tmpdir cleanup"


# ---------------------------------------------------------------------------
# 4. Generic push failure must be labeled "Git push failed", not "Clone failed"
# ---------------------------------------------------------------------------


def test_generic_push_failure_labeled_git_push_failed_not_clone_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A push error that is not a workflow-scope rejection must be labeled
    ``Git push failed`` and must NOT carry the ``Clone failed`` label.
    """
    _patch_token(monkeypatch)

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

    run_calls: list[list[str]] = []

    def fake_run(args, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        run_calls.append(list(args))
        if "clone" in args:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "push" in args:
            raise push_err
        raise AssertionError(
            f"subprocess.run was called with an unexpected command: {args!r}"
        )

    cleanup_calls: list[str] = []

    def fake_rmtree(path, *args_list, **kwargs):  # type: ignore[no-untyped-def]
        cleanup_calls.append(str(path))

    monkeypatch.setattr(f2gh.subprocess, "run", fake_run)
    monkeypatch.setattr(f2gh.shutil, "rmtree", fake_rmtree)

    with pytest.raises(SystemExit) as exc_info:
        f2gh.mirror_git_repo(
            source="owner/repo",
            target="owner/target",
            dry_run=False,
        )

    code = exc_info.value.code
    assert code is not None
    assert code != 0

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    # Must be labeled as a push failure.
    assert "Git push failed" in combined, (
        "expected 'Git push failed' in output, got:\n" + combined
    )

    # Must NOT be labeled as a clone failure.
    assert "Clone failed" not in combined, (
        "expected 'Clone failed' to NOT appear for a generic push error, "
        f"got:\n{combined}"
    )

    # No traceback leaked.
    assert "Traceback" not in combined, (
        "expected no traceback in output, got:\n" + combined
    )

    # No raw token leak.
    assert GITHUB_TOKEN_SENTINEL not in combined, (
        "raw GitHub token leaked into output:\n" + combined
    )

    # Cleanup still happens.
    assert cleanup_calls, "expected shutil.rmtree to be called for tmpdir cleanup"
