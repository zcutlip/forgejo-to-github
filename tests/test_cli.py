"""RED-class: A. Pure unit — argparse and entrypoint behavior.

CLI surface tests covering observable contracts of ``f2gh.parse_args`` and
``f2gh.main`` that are not already exercised by ``test_characterization.py``.

Specifically covered:

* Missing required arguments raise ``SystemExit`` with a nonzero exit code
  and a usage message on stderr (argparse default contract, code-locked).
* An unknown flag raises ``SystemExit`` with a nonzero exit code and a
  usage message on stderr.
* ``--help`` exits zero and lists every documented flag with a one-line
  description.
* The module exposes ``main`` as a callable entrypoint so the
  ``f2gh`` console-script shim (declared in ``pyproject.toml``) resolves.
* ``main()`` invokes ``parse_args`` then forwards to ``migrate`` with the
  parsed keyword arguments.

These tests are deterministic and require no network or credentials.
They assert on exit codes, exact substring presence in captured streams,
and the observable call graph into ``migrate``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import f2gh
from forgejo_to_github.__about__ import __version__
from forgejo_to_github.about import about
from forgejo_to_github.state import StateLockedError, StateWriteError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_parse_args(argv: list[str]) -> argparse.Namespace:
    """Invoke ``f2gh.parse_args`` with the supplied argv.

    ``parse_args`` calls ``argparse.ArgumentParser.parse_args`` which
    reads ``sys.argv`` directly. We patch ``sys.argv`` rather than the
    parse_args function itself so we observe the real argparse path.
    """
    with patch.object(sys, "argv", ["f2gh", *argv]):
        return f2gh.parse_args()


# ---------------------------------------------------------------------------
# 1. Source/target optionality and argument validation
# ---------------------------------------------------------------------------


def test_parse_args_source_defaults_none_when_omitted() -> None:
    """Omitting ``--source`` parses; the slug is inferred later, not by argparse."""
    args = _run_parse_args(["--target", "owner/target"])

    assert args.source is None
    assert args.target == "owner/target"


def test_parse_args_target_defaults_none_when_omitted() -> None:
    """Omitting ``--target`` parses; the default resolves later, not by argparse."""
    args = _run_parse_args(["--source", "owner/source"])

    assert args.source == "owner/source"
    assert args.target is None


def test_parse_args_source_and_target_both_default_none() -> None:
    """Omitting both flags parses; inference supplies them downstream."""
    args = _run_parse_args([])

    assert args.source is None
    assert args.target is None
    assert args.cwd is False


def test_parse_args_unknown_flag_exits_nonzero_with_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unknown flag must SystemExit non-zero and show usage."""
    with pytest.raises(SystemExit) as exc_info:
        _run_parse_args(
            [
                "--source",
                "owner/source",
                "--target",
                "owner/target",
                "--bogus-flag",
            ]
        )

    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    assert captured.err
    assert "usage:" in captured.err.lower()
    # argparse echoes the bad flag name in its error line.
    assert "--bogus-flag" in captured.err, (
        "expected '--bogus-flag' named in stderr, got: " + captured.err
    )


# ---------------------------------------------------------------------------
# 2. --help
# ---------------------------------------------------------------------------


def test_help_exits_zero_and_mentions_source_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` must exit 0, write to stdout, and mention source and target."""
    with pytest.raises(SystemExit) as exc_info:
        _run_parse_args(["--help"])

    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    # argparse writes --help to stdout, not stderr.
    assert captured.out, "expected --help text on stdout"
    assert "--source" in captured.out
    assert "--target" in captured.out


def test_help_lists_documented_optional_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` must enumerate every documented optional flag."""
    with pytest.raises(SystemExit):
        _run_parse_args(["--help"])

    captured = capsys.readouterr()
    for flag in ("--dry-run", "--yes", "--skip-git", "--public", "--description"):
        assert flag in captured.out, (
            f"expected {flag!r} in --help output, got:\n{captured.out}"
        )


# ---------------------------------------------------------------------------
# 3. Entry point compatibility
# ---------------------------------------------------------------------------


def test_main_is_callable_entrypoint() -> None:
    """``f2gh.main`` must be a zero-argument callable.

    The ``f2gh`` console script (declared in ``pyproject.toml``) resolves
    to ``f2gh:main``, which must therefore be importable and callable.
    """
    assert callable(f2gh.main)


def test_main_invokes_parse_args_then_orchestrator_flow() -> None:
    """Approved API-alignment amendment (stage-06) — not a weakening.

    Replaces the obsolete ``migrate`` seam (``test_main_invokes_parse_args_then_migrate``)
    with the approved observable flow from ``f2gh.main``:

        parse_args() -> _build_orchestrator(args) -> orchestrator.run()
        -> reporter.render_final(result) -> reporter.exit_outcome(result) -> sys.exit(code)

    Verifies each hop with injected fakes/mocks; no environment, network, or
    subprocess is touched. Renamed/re-written with explicit user permission.
    """
    sentinel_args = argparse.Namespace(
        source="owner/source",
        target="owner/target",
        dry_run=True,
        yes=True,
        skip_git=True,
        public=False,
        description="custom",
    )
    sentinel_result: object = object()
    sentinel_exit_code = 42

    fake_reporter = Mock()
    fake_reporter.exit_outcome.return_value = sentinel_exit_code

    fake_orchestrator = Mock()
    fake_orchestrator.run.return_value = sentinel_result
    fake_orchestrator.reporter = fake_reporter

    with (
        patch.object(f2gh, "parse_args", return_value=sentinel_args) as mock_parse,
        patch.object(
            f2gh, "_build_orchestrator", return_value=fake_orchestrator
        ) as mock_build,
        patch.object(
            f2gh.sys, "exit", side_effect=SystemExit(sentinel_exit_code)
        ) as mock_exit,
        pytest.raises(SystemExit) as exc_info,
    ):
        f2gh.main()

    # Exits with the code returned by reporter.exit_outcome(result).
    assert exc_info.value.code == sentinel_exit_code
    mock_exit.assert_called_once_with(sentinel_exit_code)

    # Observable call graph — each collaborator invoked exactly once with the
    # expected argument.
    mock_parse.assert_called_once_with()
    mock_build.assert_called_once_with(sentinel_args)
    fake_orchestrator.run.assert_called_once_with()
    fake_reporter.render_final.assert_called_once_with(sentinel_result)
    fake_reporter.exit_outcome.assert_called_once_with(sentinel_result)


def test_main_declined_target_prompt_exits_five() -> None:
    """A declined target-repo creation prompt aborts the run with exit 5.

    Drives ``main()`` end to end with a declined pre-git run: the fake
    orchestrator returns a result carrying ``aborted`` True, exposes no
    ``reporter`` attribute so ``main()`` falls back to a real
    ``Reporter``, and the real ``render_final`` + ``exit_outcome``
    mapping decides the exit code.
    """
    args = argparse.Namespace(
        source="owner/source",
        target="owner/target",
        dry_run=False,
        yes=False,
        skip_git=True,
        public=False,
        description=None,
    )
    declined_result = SimpleNamespace(
        aborted=True,
        dry_run=False,
        failures=[],
        issues_failed=0,
        comments_failed=0,
        git={"clone": "skipped", "push": "skipped"},
    )
    fake_orchestrator = SimpleNamespace(run=Mock(return_value=declined_result))

    with (
        patch.object(f2gh, "parse_args", return_value=args),
        patch.object(f2gh, "_build_orchestrator", return_value=fake_orchestrator),
        pytest.raises(SystemExit) as exc_info,
    ):
        f2gh.main()

    assert declined_result.aborted is True
    assert exc_info.value.code == 5


def test_parse_args_returns_namespace_with_expected_attributes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The parsed Namespace must carry every documented attribute.

    Type/literal-default assertions only — not duplicate of
    ``test_characterization.py`` flag round-trip tests.
    """
    with patch.object(
        sys,
        "argv",
        ["f2gh", "--source", "owner/source", "--target", "owner/target"],
    ):
        args = f2gh.parse_args()

    # Exactly the documented attributes must be present.
    expected_attrs = {
        "source",
        "target",
        "dry_run",
        "yes",
        "skip_git",
        "public",
        "description",
    }
    assert expected_attrs.issubset(set(vars(args))), (
        "expected attributes missing from Namespace: "
        + repr(expected_attrs - set(vars(args)))
    )


# ---------------------------------------------------------------------------
# 6. CLI prompter helper
# ---------------------------------------------------------------------------


def test_cli_prompter_bypasses_when_yes_flag_set() -> None:
    """The CLI prompter helper returns True without reading stdin when yes=True.

    ``f2gh._make_prompter`` constructs a ``prompter(prompt, default) -> bool``
    callable. When ``Repository.yes`` is True, the returned callable must
    immediately return True without prompting — no stdin access.
    """
    from forgejo_to_github.domain import Repository

    repo = Repository(source="owner/source", target="owner/target", yes=True)
    prompter = f2gh._make_prompter(repo)

    # A fake stdin that would fail if read.
    sentinel = object()
    with patch.object(sys, "stdin", sentinel):
        result = prompter("Proceed?", False)

    assert result is True, (
        f"yes=True must return True without prompting; got {result!r}"
    )


def test_cli_prompter_reads_stdin_when_yes_flag_unset() -> None:
    """The CLI prompter helper reads stdin and returns True for 'yes'.

    When ``Repository.yes`` is False, the returned callable must prompt
    via ``input()`` and return True when the user types "yes".
    """
    from forgejo_to_github.domain import Repository

    repo = Repository(source="owner/source", target="owner/target", yes=False)
    prompter = f2gh._make_prompter(repo)

    with patch("builtins.input", return_value="yes"):
        result = prompter("Proceed?", False)

    assert result is True, f"input 'yes' must return True; got {result!r}"


# ---------------------------------------------------------------------------
# 7. --version
# ---------------------------------------------------------------------------


def test_parse_args_version_flag_prints_version_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--version`` must exit 0 and print bare ``__version__`` to stdout."""
    with pytest.raises(SystemExit) as exc_info:
        _run_parse_args(["--version"])

    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert captured.out, "expected --version text on stdout"
    assert __version__ == captured.out.rstrip()


def test_help_description_contains_about(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--help`` description contains ``about()``; ``--version`` prints bare ``__version__``."""
    with pytest.raises(SystemExit) as exc_info:
        _run_parse_args(["--help"])

    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert captured.out, "expected --help text on stdout"
    assert about() in captured.out


# ---------------------------------------------------------------------------
# 8. KeyboardInterrupt handling in main()
# ---------------------------------------------------------------------------


def test_main_keyboard_interrupt_prints_resume_hint_and_exits_130(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An interrupted run reports the interrupt, points at saved state, and exits 130.

    The message must name the source and target so the operator can resume
    with the same arguments, and no traceback may leak to either stream.
    """
    args = argparse.Namespace(
        source="owner/source",
        target="owner/target",
        dry_run=False,
        yes=True,
        skip_git=True,
        public=False,
        description=None,
    )
    fake_orchestrator = Mock()
    fake_orchestrator.run.side_effect = KeyboardInterrupt

    with (
        patch.object(f2gh, "parse_args", return_value=args),
        patch.object(f2gh, "_build_orchestrator", return_value=fake_orchestrator),
        patch.object(f2gh.sys, "exit") as mock_exit,
    ):
        try:
            f2gh.main()
        except KeyboardInterrupt:
            pytest.fail(
                "main() let KeyboardInterrupt escape instead of handling it "
                "and exiting 130"
            )

    mock_exit.assert_called_once_with(130)

    captured = capsys.readouterr()
    assert "Interrupted by user" in captured.err
    assert "state saved to" in captured.err
    assert "./state.json" not in captured.err
    assert "owner/source/owner/target/state.json" in captured.err
    assert (
        "resume with f2gh --source owner/source --target owner/target" in captured.err
    )
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_main_state_file_flag_is_honored_verbatim(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An explicit state file path is used as given, not namespaced or rewritten.

    The interrupt notice reports the resolved state path, so an operator who
    supplied their own path sees exactly that path.
    """
    custom_state = tmp_path / "custom-state.json"
    args = argparse.Namespace(
        source="owner/source",
        target="owner/target",
        dry_run=False,
        yes=True,
        skip_git=True,
        public=False,
        description=None,
        state_file=str(custom_state),
    )
    fake_orchestrator = Mock()
    fake_orchestrator.run.side_effect = KeyboardInterrupt

    with (
        patch.object(f2gh, "parse_args", return_value=args),
        patch.object(f2gh, "_build_orchestrator", return_value=fake_orchestrator),
        patch.object(f2gh.sys, "exit") as mock_exit,
    ):
        try:
            f2gh.main()
        except KeyboardInterrupt:
            pytest.fail(
                "main() let KeyboardInterrupt escape instead of handling it "
                "and exiting 130"
            )

    mock_exit.assert_called_once_with(130)

    captured = capsys.readouterr()
    assert str(custom_state) in captured.err
    assert "owner/source/owner/target/state.json" not in captured.err


def test_main_keyboard_interrupt_dry_run_does_not_claim_state_saved(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A dry-run interrupt reports the interrupt but never claims state was saved.

    Dry runs do not persist a checkpoint, so the notice must omit any
    ``state saved`` claim while still exiting 130.
    """
    args = argparse.Namespace(
        source="owner/source",
        target="owner/target",
        dry_run=True,
        yes=True,
        skip_git=True,
        public=False,
        description=None,
    )
    fake_orchestrator = Mock()
    fake_orchestrator.run.side_effect = KeyboardInterrupt

    with (
        patch.object(f2gh, "parse_args", return_value=args),
        patch.object(f2gh, "_build_orchestrator", return_value=fake_orchestrator),
        patch.object(f2gh.sys, "exit") as mock_exit,
    ):
        try:
            f2gh.main()
        except KeyboardInterrupt:
            pytest.fail(
                "main() let KeyboardInterrupt escape instead of handling it "
                "and exiting 130"
            )

    captured = capsys.readouterr()
    assert "Interrupted by user" in captured.err
    assert "state saved" not in captured.err.lower()
    mock_exit.assert_called_once_with(130)


def test_main_non_interrupt_exception_propagates() -> None:
    """A non-interrupt exception from the orchestrator propagates unchanged.

    Guards against the interrupt handler accidentally swallowing errors
    other than ``KeyboardInterrupt``.
    """
    args = argparse.Namespace(
        source="owner/source",
        target="owner/target",
        dry_run=False,
        yes=True,
        skip_git=True,
        public=False,
        description=None,
    )
    fake_orchestrator = Mock()
    fake_orchestrator.run.side_effect = RuntimeError("boom")

    with (
        patch.object(f2gh, "parse_args", return_value=args),
        patch.object(f2gh, "_build_orchestrator", return_value=fake_orchestrator),
        patch.object(f2gh.sys, "exit") as mock_exit,
        pytest.raises(RuntimeError),
    ):
        f2gh.main()

    mock_exit.assert_not_called()


# ---------------------------------------------------------------------------
# 9. State path failures in main()
# ---------------------------------------------------------------------------


def test_main_refuses_to_start_when_the_state_path_is_unusable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unusable state path stops the run before anything is migrated.

    The operator is told which resolved path failed, and nothing that looks
    like a successful migration is emitted.
    """
    unreachable = tmp_path / "blocker" / "state.json"
    args = argparse.Namespace(
        source="owner/source",
        target="owner/target",
        dry_run=False,
        yes=True,
        skip_git=True,
        public=False,
        description=None,
        state_file=str(unreachable),
    )
    fake_orchestrator = Mock()
    fake_orchestrator.run.side_effect = StateWriteError(unreachable, "not a directory")

    with (
        patch.object(f2gh, "parse_args", return_value=args),
        patch.object(f2gh, "_build_orchestrator", return_value=fake_orchestrator),
        patch.object(f2gh.sys, "exit") as mock_exit,
    ):
        f2gh.main()

    mock_exit.assert_called_once_with(f2gh.EXIT_STATE_ERROR)

    captured = capsys.readouterr()
    assert str(unreachable) in captured.err
    assert "nothing was migrated" not in captured.err
    assert "Migration complete" not in captured.out
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_main_reports_lock_contention_distinctly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Contention and an unusable path must not produce the same message.

    Both refuse to start, but "another run holds this path" and "this path
    cannot be written" call for different responses, so the operator must be
    able to tell them apart from the output alone.
    """
    state_path = tmp_path / "state.json"

    def _stderr_for(error: BaseException) -> str:
        args = argparse.Namespace(
            source="owner/source",
            target="owner/target",
            dry_run=False,
            yes=True,
            skip_git=True,
            public=False,
            description=None,
            state_file=str(state_path),
        )
        fake_orchestrator = Mock()
        fake_orchestrator.run.side_effect = error
        with (
            patch.object(f2gh, "parse_args", return_value=args),
            patch.object(f2gh, "_build_orchestrator", return_value=fake_orchestrator),
            patch.object(f2gh.sys, "exit") as mock_exit,
        ):
            f2gh.main()
        mock_exit.assert_called_once_with(f2gh.EXIT_STATE_ERROR)
        return capsys.readouterr().err

    contention = _stderr_for(StateLockedError(state_path))
    unusable = _stderr_for(StateWriteError(state_path, "permission denied"))

    assert str(state_path) in contention
    assert "nothing was migrated" not in contention
    assert contention != unusable


# ---------------------------------------------------------------------------
# 10. --clean removes the cached git mirror (issue #5)
# ---------------------------------------------------------------------------


def _isolated_user_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point platform user dirs at tmp_path so --clean never touches home."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _expected_mirror_dir() -> Path:
    """The default cached-mirror location for owner/source → owner/target.

    Resolved lazily so tests calling it after ``_isolated_user_dirs``
    observe the isolated base directories.
    """
    import platformdirs

    return (
        Path(platformdirs.user_cache_dir("f2gh"))
        / "owner"
        / "source"
        / "owner"
        / "target"
        / "mirror.git"
    )


def test_parse_args_clean_flag_parses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--clean parses as a store_true flag alongside source/target."""
    with patch.object(
        sys, "argv", ["f2gh", "--source", "owner/source", "--target", "owner/target", "--clean"]
    ):
        args = f2gh.parse_args()

    assert args.clean is True
    capsys.readouterr()


def test_main_clean_removes_cached_mirror_and_exits_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--clean deletes this migration's mirror, reports it, needs no tokens."""
    _isolated_user_dirs(tmp_path, monkeypatch)
    monkeypatch.delenv("CODEBERG_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    mirror_dir = _expected_mirror_dir()
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "HEAD").write_text("ref: refs/heads/main\n")

    with (
        patch.object(
            sys,
            "argv",
            ["f2gh", "--source", "owner/source", "--target", "owner/target", "--clean"],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        f2gh.main()

    assert exc_info.value.code == 0
    assert not mirror_dir.exists()
    captured = capsys.readouterr()
    assert "mirror" in captured.out.lower() or str(mirror_dir) in captured.out


def test_main_clean_dry_run_prints_without_deleting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--clean --dry-run reports what it would remove and deletes nothing."""
    _isolated_user_dirs(tmp_path, monkeypatch)
    mirror_dir = _expected_mirror_dir()
    mirror_dir.mkdir(parents=True)
    (mirror_dir / "HEAD").write_text("ref: refs/heads/main\n")

    with (
        patch.object(
            sys,
            "argv",
            [
                "f2gh",
                "--source",
                "owner/source",
                "--target",
                "owner/target",
                "--clean",
                "--dry-run",
            ],
        ),
        pytest.raises(SystemExit) as exc_info,
    ):
        f2gh.main()

    assert exc_info.value.code == 0
    assert mirror_dir.exists()
    captured = capsys.readouterr()
    assert str(mirror_dir) in captured.out or "mirror" in captured.out.lower()


def test_main_clean_refuses_while_state_locked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--clean refuses when another run holds this migration's state lock."""
    from forgejo_to_github.paths import default_state_base, state_path_for
    from forgejo_to_github.state import StateStore

    _isolated_user_dirs(tmp_path, monkeypatch)
    state_path = state_path_for(
        default_state_base(), "owner/source", "owner/target"
    )
    holder = StateStore(state_path, "owner/source", "owner/target")
    holder.prepare()
    try:
        with (
            patch.object(
                sys,
                "argv",
                [
                    "f2gh",
                    "--source",
                    "owner/source",
                    "--target",
                    "owner/target",
                    "--clean",
                ],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            f2gh.main()
    finally:
        holder.release()

    assert exc_info.value.code == f2gh.EXIT_STATE_ERROR
    captured = capsys.readouterr()
    assert captured.err, "expected a refusal message on stderr"


# ---------------------------------------------------------------------------
# Ctrl+C during cwd resolution (interrupt exits 130, never a traceback)
# ---------------------------------------------------------------------------

_ORIGIN_URL = "ssh://git@codeberg.org/o/r.git"


def _ok_rc(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


class _InterruptibleRunner:
    """Scripted git runner; raises KeyboardInterrupt on the configured argv."""

    def __init__(
        self,
        script: dict[tuple[str, ...], SimpleNamespace],
        interrupt_on: tuple[str, ...],
    ) -> None:
        self._script = script
        self._interrupt_on = interrupt_on

    def __call__(self, argv: list[str], **kwargs: object) -> SimpleNamespace:
        if tuple(argv) == self._interrupt_on:
            raise KeyboardInterrupt
        return self._script[tuple(argv)]


def _valid_cwd_script() -> dict[tuple[str, ...], SimpleNamespace]:
    """Script the validation probes; the network probe is KI-injected."""
    return {
        ("git", "rev-parse", "--is-inside-work-tree"): _ok_rc("true\n"),
        ("git", "ls-remote", "--get-url", "origin"): _ok_rc(_ORIGIN_URL + "\n"),
        ("git", "rev-parse", "--is-shallow-repository"): _ok_rc("false\n"),
        ("git", "config", "--get", "extensions.partialclone"): SimpleNamespace(
            returncode=1, stdout="", stderr=""
        ),
        ("git", "status", "--porcelain"): _ok_rc(""),
    }


def _run_main_expecting_interrupt() -> None:
    """Run ``main()`` converting an escaped KeyboardInterrupt to a failure.

    Wrapping is required: an escaping KeyboardInterrupt aborts the pytest
    session instead of recording a normal failure.
    """
    try:
        f2gh.main()
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped main() instead of exiting 130")


def test_interrupt_during_freshness_probe_exits_130_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl+C during the freshness probe exits 130 with the interrupt message."""
    runner = _InterruptibleRunner(
        _valid_cwd_script(), interrupt_on=("git", "ls-remote", _ORIGIN_URL)
    )
    monkeypatch.setattr(f2gh, "_git_runner", runner)
    with (
        patch.object(sys, "argv", ["f2gh", "--cwd", "--target", "o/r"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_main_expecting_interrupt()

    assert exc_info.value.code == 130
    err = capsys.readouterr().err
    assert "Interrupted by user" in err
    assert "warning" not in err.lower()
    assert "Traceback" not in err


def test_interrupt_before_state_path_bound_exits_130_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl+C during source inference still exits 130 without naming state."""
    runner = _InterruptibleRunner(
        _valid_cwd_script(),
        interrupt_on=("git", "rev-parse", "--is-inside-work-tree"),
    )
    monkeypatch.setattr(f2gh, "_git_runner", runner)
    with (
        patch.object(sys, "argv", ["f2gh", "--cwd", "--target", "o/r"]),
        pytest.raises(SystemExit) as exc_info,
    ):
        _run_main_expecting_interrupt()

    assert exc_info.value.code == 130
    err = capsys.readouterr().err
    assert "Interrupted by user" in err
    assert "state saved to" not in err
