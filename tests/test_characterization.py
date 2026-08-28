"""Characterization tests locking down existing/desired contracts for f2gh.

These tests pin behavior of state persistence, markdown formatting, and CLI
parsing so that the package refactor in plans/02 does not silently change them.

Conventions:
- STATE_FILE is a module-level string on f2gh. Tests monkeypatch it to a
  tmp_path location so they do not touch the real working directory.
- Tests use exact deterministic assertions (no regex, no partial match) so a
  drift in formatting or JSON shape fails loudly.
"""

import argparse
import json
import sys

import f2gh

# --- 1. load_state returns fresh defaults on source/target mismatch ---


def test_load_state_returns_fresh_defaults_when_source_mismatches(
    monkeypatch, tmp_path
):
    """A state.json pointing at a different source must be discarded entirely."""
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "source": "other/source",
                "target": "other/target",
                "repo_created": True,
                "git_pushed": True,
                "migrated": {"1": 99, "2": 100},
            }
        )
    )
    monkeypatch.setattr(f2gh, "STATE_FILE", str(state_path))

    loaded = f2gh.load_state("owner/source", "owner/target")

    assert loaded == {
        "repo_created": False,
        "git_pushed": False,
        "migrated": {},
    }


def test_load_state_returns_fresh_defaults_when_target_mismatches(
    monkeypatch, tmp_path
):
    """Source match but target mismatch must also discard state."""
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "source": "owner/source",
                "target": "other/target",
                "repo_created": True,
                "git_pushed": True,
                "migrated": {"1": 99},
            }
        )
    )
    monkeypatch.setattr(f2gh, "STATE_FILE", str(state_path))

    loaded = f2gh.load_state("owner/source", "owner/target")

    assert loaded == {
        "repo_created": False,
        "git_pushed": False,
        "migrated": {},
    }


def test_load_state_returns_fresh_defaults_when_no_state_file(monkeypatch, tmp_path):
    """Missing state.json must yield fresh defaults (no FileNotFoundError)."""
    state_path = tmp_path / "state.json"  # never created
    monkeypatch.setattr(f2gh, "STATE_FILE", str(state_path))

    loaded = f2gh.load_state("owner/source", "owner/target")

    assert loaded == {
        "repo_created": False,
        "git_pushed": False,
        "migrated": {},
    }


# --- 2. save_state writes via atomic os.replace ---


def test_save_state_uses_os_replace_for_atomic_write(monkeypatch, tmp_path):
    """save_state must call os.replace(tmp, dest) so writes are atomic."""
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(f2gh, "STATE_FILE", str(state_path))

    real_replace = f2gh.os.replace
    calls: list[tuple[str, str]] = []

    def spy_replace(src: str, dst: str) -> None:
        calls.append((src, dst))
        real_replace(src, dst)

    monkeypatch.setattr(f2gh.os, "replace", spy_replace)

    f2gh.save_state(
        "owner/source",
        "owner/target",
        repo_created=True,
        git_pushed=False,
        migrated={12: 34},
    )

    expected_tmp = str(tmp_path / "state.json.tmp")
    expected_dest = str(tmp_path / "state.json")

    assert len(calls) == 1, f"os.replace call count: {len(calls)}"
    assert calls[0] == (expected_tmp, expected_dest)

    # Final on-disk shape after the atomic replacement.
    assert json.loads(state_path.read_text()) == {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": True,
        "git_pushed": False,
        "migrated": {"12": 34},
    }
    # No leftover .tmp file — atomic replace should have moved it away.
    assert not (tmp_path / "state.json.tmp").exists()


# --- 3. format_issue_body markdown structure ---


def test_format_issue_body_preserves_author_date_and_body():
    """format_issue_body must emit a fixed markdown block with all fields."""
    body = f2gh.format_issue_body(
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
    body = f2gh.format_issue_body(
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


# --- 4. format_comment_body preserves author/date/body ---


def test_format_comment_body_preserves_author_date_and_body():
    """format_comment_body must emit a fixed markdown blockquote for a comment."""
    body = f2gh.format_comment_body(
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
    body = f2gh.format_comment_body(
        author="dave",
        date="2024-04-04",
        body=None,
    )

    expected = "> **@dave** commented on 2024-04-04:\n\n"
    assert body == expected


# --- 5. parse_args accepts the required + optional flags ---


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
