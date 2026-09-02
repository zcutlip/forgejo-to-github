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
from unittest.mock import Mock, patch

import pytest

import f2gh

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _run_parse_args(argv: list[str]) -> None:
    """Invoke ``f2gh.parse_args`` with the supplied argv.

    ``parse_args`` calls ``argparse.ArgumentParser.parse_args`` which
    reads ``sys.argv`` directly. We patch ``sys.argv`` rather than the
    parse_args function itself so we observe the real argparse path.
    """
    with patch.object(sys, "argv", ["f2gh", *argv]):
        f2gh.parse_args()


# ---------------------------------------------------------------------------
# 1. Missing required arguments
# ---------------------------------------------------------------------------


def test_parse_args_missing_source_exits_nonzero_with_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting ``--source`` must SystemExit with code 2 and a usage message."""
    with pytest.raises(SystemExit) as exc_info:
        _run_parse_args(["--target", "owner/target"])

    # argparse uses exit code 2 for argument-validation failures.
    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    # argparse writes the usage banner to stderr.
    assert captured.err, "expected argparse to write usage to stderr"
    assert "usage:" in captured.err.lower(), (
        "expected 'usage:' in stderr, got: " + captured.err
    )
    # argparse names the offending flag in the error line.
    assert "--source" in captured.err, (
        "expected '--source' in stderr error message, got: " + captured.err
    )


def test_parse_args_missing_target_exits_nonzero_with_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting ``--target`` must SystemExit with code 2 and a usage message."""
    with pytest.raises(SystemExit) as exc_info:
        _run_parse_args(["--source", "owner/source"])

    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    assert captured.err
    assert "usage:" in captured.err.lower()
    assert "--target" in captured.err, (
        "expected '--target' in stderr error message, got: " + captured.err
    )


def test_parse_args_missing_both_required_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Omitting both required flags must SystemExit non-zero and show usage."""
    with pytest.raises(SystemExit) as exc_info:
        _run_parse_args([])

    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    assert "usage:" in captured.err.lower()
    # Either or both flags may be referenced; the test only requires usage text.
    assert ("--source" in captured.err) or ("--target" in captured.err), (
        "expected at least one required flag named in stderr, got: " + captured.err
    )


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
