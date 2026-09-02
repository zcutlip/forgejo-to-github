"""Characterization tests locking down formatting and CLI contracts.

Stage-06 alignment: this module previously asserted ``f2gh.load_state`` /
``f2gh.save_state`` / ``f2gh.STATE_FILE``. Those symbols are removed in
stage-06; the ``StateStore`` contract is now pinned by
``tests/test_state_store.py``. The four state tests are deleted here and
not re-added — they are redundant with the dedicated ``StateStore`` tests.

Removed legacy tests (behavior directly covered by ``test_state_store.py``):
- ``test_load_state_returns_fresh_defaults_when_source_mismatches``
- ``test_load_state_returns_fresh_defaults_when_target_mismatches``
- ``test_load_state_returns_fresh_defaults_when_no_state_file``
- ``test_save_state_uses_os_replace_for_atomic_write``

Preserved contracts:
- Markdown formatting via ``forgejo_to_github.formatting`` (pure functions,
  exact string assertions, no regex).
- CLI parsing via ``f2gh.parse_args`` (required/optional flags, exact defaults).

No network, no credentials, no filesystem beyond ``tmp_path`` where needed.
"""

import argparse
import sys

import f2gh
from forgejo_to_github.formatting import format_comment_body, format_issue_body

# --- 1. format_issue_body markdown structure ---


def test_format_issue_body_preserves_author_date_and_body():
    """format_issue_body must emit a fixed markdown block with all fields."""
    body = format_issue_body(
        source="owner/source",
        cb_index=42,
        author="alice",
        date="2024-01-15",
        body="Original issue body text.\n\nLine two.",
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #42]"
        "(https://codeberg.org/owner/source/issues/42))\n"
        "> **Author:** @alice | **Date:** 2024-01-15\n\n"
        "Original issue body text.\n\nLine two."
    )
    assert body == expected


def test_format_issue_body_handles_none_body():
    """format_issue_body must not crash and must emit empty body when None."""
    body = format_issue_body(
        source="owner/source",
        cb_index=7,
        author="bob",
        date="2024-02-02",
        body=None,
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #7]"
        "(https://codeberg.org/owner/source/issues/7))\n"
        "> **Author:** @bob | **Date:** 2024-02-02\n\n"
        ""
    )
    assert body == expected


# --- 2. format_comment_body preserves author/date/body ---


def test_format_comment_body_preserves_author_date_and_body():
    """format_comment_body must emit a fixed markdown blockquote for a comment."""
    body = format_comment_body(
        author="carol",
        date="2024-03-03",
        body="Comment body line one.\nLine two.",
    )

    expected = (
        "> **@carol** commented on 2024-03-03:\n\nComment body line one.\nLine two."
    )
    assert body == expected


def test_format_comment_body_handles_none_body():
    """format_comment_body must not crash when body is None."""
    body = format_comment_body(
        author="dave",
        date="2024-04-04",
        body=None,
    )

    expected = "> **@dave** commented on 2024-04-04:\n\n"
    assert body == expected


# --- 3. parse_args accepts the required + optional flags ---


def _parse_with_argv(monkeypatch, argv: list[str]) -> argparse.Namespace:
    monkeypatch.setattr(sys, "argv", ["f2gh", *argv])
    return f2gh.parse_args()


def test_parse_args_minimum_required_source_target(monkeypatch):
    """Only --source and --target are required; everything else defaults."""
    args = _parse_with_argv(
        monkeypatch,
        ["--source", "owner/source", "--target", "owner/target"],
    )

    assert isinstance(args, argparse.Namespace)
    assert args.source == "owner/source"
    assert args.target == "owner/target"
    assert args.dry_run is False
    assert args.yes is False
    assert args.skip_git is False
    assert args.public is False
    assert args.description is None


def test_parse_args_dry_run_flag(monkeypatch):
    args = _parse_with_argv(
        monkeypatch,
        [
            "--source",
            "owner/source",
            "--target",
            "owner/target",
            "--dry-run",
        ],
    )

    assert args.dry_run is True


def test_parse_args_yes_flag(monkeypatch):
    args = _parse_with_argv(
        monkeypatch,
        [
            "--source",
            "owner/source",
            "--target",
            "owner/target",
            "--yes",
        ],
    )

    assert args.yes is True


def test_parse_args_skip_git_flag(monkeypatch):
    args = _parse_with_argv(
        monkeypatch,
        [
            "--source",
            "owner/source",
            "--target",
            "owner/target",
            "--skip-git",
        ],
    )

    assert args.skip_git is True


def test_parse_args_public_flag(monkeypatch):
    args = _parse_with_argv(
        monkeypatch,
        [
            "--source",
            "owner/source",
            "--target",
            "owner/target",
            "--public",
        ],
    )

    assert args.public is True


def test_parse_args_description_value(monkeypatch):
    args = _parse_with_argv(
        monkeypatch,
        [
            "--source",
            "owner/source",
            "--target",
            "owner/target",
            "--description",
            "A custom repo description",
        ],
    )

    assert args.description == "A custom repo description"


def test_parse_args_all_flags_combined(monkeypatch):
    """All flags together must round-trip their values exactly."""
    args = _parse_with_argv(
        monkeypatch,
        [
            "--source",
            "owner/source",
            "--target",
            "owner/target",
            "--dry-run",
            "--yes",
            "--skip-git",
            "--public",
            "--description",
            "desc text",
        ],
    )

    assert args.source == "owner/source"
    assert args.target == "owner/target"
    assert args.dry_run is True
    assert args.yes is True
    assert args.skip_git is True
    assert args.public is True
    assert args.description == "desc text"
