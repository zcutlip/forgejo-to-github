# RED class: B. Boundary unit

"""RED-stage boundary unit tests for repository-description handling in
``f2gh.migrate()`` Phase 1.

These tests pin the observable contract of how the GitHub target repo's
description is sourced and supplied to ``create_github_repo``:

* An explicit ``description`` argument is passed straight through to
  ``create_github_repo`` without any Codeberg metadata fetch.
* When ``description`` is ``None`` and Codeberg returns a non-empty
  ``description`` field, that value is passed to ``create_github_repo``.
* When ``description`` is ``None`` and Codeberg returns an empty /
  missing ``description`` field, the literal fallback
  ``"Migrated from Codeberg"`` is passed to ``create_github_repo``.
* When ``description`` is ``None`` and Codeberg metadata fetch raises
  ``requests.HTTPError`` / ``ConnectionError`` / ``Timeout``, the same
  literal fallback is passed (current raw-exception handling, no
  structured error translation).
* An already-existing target repo must not fetch Codeberg description
  and must not call ``create_github_repo``.
* ``--dry-run`` must not call ``create_github_repo`` and must not
  mutate any description state.

All network and subprocess boundaries are mocked. ``STATE_FILE`` is
redirected to ``tmp_path`` for every test.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
import requests

import f2gh

# Minimal existing-target fixture used by Phase-1-skip tests. The
# ``open_issues_count`` is set to 0 so migrate() does not prompt for
# confirmation under non-yes runs.
_EXISTING_TARGET: dict[str, object] = {
    "id": 12345,
    "name": "target",
    "full_name": "owner/target",
    "open_issues_count": 0,
}

_NO_ISSUES: list[dict[str, object]] = []


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Redirect STATE_FILE to tmp_path; never touch the repo's real state."""
    monkeypatch.setattr(f2gh, "STATE_FILE", str(tmp_path / "state.json"))
    # Skip the per-issue ``time.sleep`` throttle to keep tests fast.
    monkeypatch.setattr(f2gh.time, "sleep", lambda *_a, **_kw: None)


def _migrate_kwargs(
    *,
    description: str | None = None,
    dry_run: bool = False,
    skip_git: bool = True,
    public: bool = False,
) -> dict[str, object]:
    """Build the standard ``migrate()`` kwargs used by every test."""
    return {
        "source": "owner/source",
        "target": "owner/target",
        "dry_run": dry_run,
        "yes": True,
        "skip_git": skip_git,
        "public": public,
        "description": description,
    }


# ---------------------------------------------------------------------------
# 1. Explicit --description is passed straight through to create_github_repo
# ---------------------------------------------------------------------------


def test_explicit_description_passed_to_create_github_repo():
    """When ``description`` is supplied on the CLI, ``create_github_repo``
    must be invoked with exactly that description and Codeberg must not
    be consulted.
    """
    explicit = "A custom description from the CLI"

    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(f2gh, "fetch_codeberg_description") as mock_fetch_desc,
        patch.object(
            f2gh, "create_github_repo", return_value={"id": 1, "name": "target"}
        ) as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=explicit))  # type: ignore[arg-type]

    # Codeberg metadata must NOT be fetched when the user gave an explicit
    # description — the orchestrator must short-circuit before the fetch.
    mock_fetch_desc.assert_not_called()

    # create_github_repo must be called once with the explicit description.
    assert mock_create.call_count == 1
    args, kwargs = mock_create.call_args
    # Description may be passed positionally or by keyword; accept either.
    if args:
        assert args[0] == "owner/target"
        assert args[1] == explicit
    else:
        assert kwargs.get("target") == "owner/target"
        assert kwargs.get("description") == explicit


# ---------------------------------------------------------------------------
# 2. Non-empty Codeberg description is used when creating the target
# ---------------------------------------------------------------------------


def test_codeberg_non_empty_description_passed_to_create_github_repo():
    """When ``description`` is ``None`` and Codeberg returns a non-empty
    string, that string is forwarded to ``create_github_repo``.
    """
    cb_desc = "Imported from Codeberg with the original description."

    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(
            f2gh, "fetch_codeberg_description", return_value=cb_desc
        ) as mock_fetch_desc,
        patch.object(
            f2gh, "create_github_repo", return_value={"id": 1, "name": "target"}
        ) as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=None))  # type: ignore[arg-type]

    mock_fetch_desc.assert_called_once_with("owner/source")

    assert mock_create.call_count == 1
    args, kwargs = mock_create.call_args
    if args:
        assert args[1] == cb_desc
    else:
        assert kwargs.get("description") == cb_desc


# ---------------------------------------------------------------------------
# 3. Empty / missing Codeberg description falls back to a literal string
# ---------------------------------------------------------------------------


def test_codeberg_empty_description_falls_back_to_default():
    """When ``description`` is ``None`` and Codeberg returns an empty
    string, ``fetch_codeberg_description`` yields ``"Migrated from
    Codeberg"`` and that fallback is forwarded to ``create_github_repo``.
    """
    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(
            f2gh,
            "fetch_codeberg_description",
            return_value="Migrated from Codeberg",
        ) as mock_fetch_desc,
        patch.object(
            f2gh, "create_github_repo", return_value={"id": 1, "name": "target"}
        ) as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=None))  # type: ignore[arg-type]

    mock_fetch_desc.assert_called_once_with("owner/source")

    assert mock_create.call_count == 1
    args, kwargs = mock_create.call_args
    if args:
        assert args[1] == "Migrated from Codeberg"
    else:
        assert kwargs.get("description") == "Migrated from Codeberg"


# ---------------------------------------------------------------------------
# 4. Metadata fetch failure falls back to the literal default
# ---------------------------------------------------------------------------


def test_codeberg_metadata_http_error_falls_back_to_default():
    """When ``fetch_codeberg_description`` raises ``requests.HTTPError``,
    the description used by ``create_github_repo`` is the literal
    fallback ``"Migrated from Codeberg"``.
    """
    err = requests.HTTPError("500 Server Error")

    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(
            f2gh, "fetch_codeberg_description", side_effect=err
        ) as mock_fetch_desc,
        patch.object(
            f2gh, "create_github_repo", return_value={"id": 1, "name": "target"}
        ) as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=None))  # type: ignore[arg-type]

    mock_fetch_desc.assert_called_once_with("owner/source")

    assert mock_create.call_count == 1
    args, kwargs = mock_create.call_args
    if args:
        assert args[1] == "Migrated from Codeberg"
    else:
        assert kwargs.get("description") == "Migrated from Codeberg"


def test_codeberg_metadata_connection_error_falls_back_to_default():
    """``requests.ConnectionError`` from metadata fetch falls back to
    ``"Migrated from Codeberg"``.
    """
    err = requests.ConnectionError("Could not resolve host codeberg.org")

    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(f2gh, "fetch_codeberg_description", side_effect=err),
        patch.object(
            f2gh, "create_github_repo", return_value={"id": 1, "name": "target"}
        ) as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=None))  # type: ignore[arg-type]

    assert mock_create.call_count == 1
    args, kwargs = mock_create.call_args
    if args:
        assert args[1] == "Migrated from Codeberg"
    else:
        assert kwargs.get("description") == "Migrated from Codeberg"


def test_codeberg_metadata_timeout_falls_back_to_default():
    """``requests.Timeout`` from metadata fetch falls back to
    ``"Migrated from Codeberg"``.
    """
    err = requests.Timeout("Read timed out")

    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(f2gh, "fetch_codeberg_description", side_effect=err),
        patch.object(
            f2gh, "create_github_repo", return_value={"id": 1, "name": "target"}
        ) as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=None))  # type: ignore[arg-type]

    assert mock_create.call_count == 1
    args, kwargs = mock_create.call_args
    if args:
        assert args[1] == "Migrated from Codeberg"
    else:
        assert kwargs.get("description") == "Migrated from Codeberg"


# ---------------------------------------------------------------------------
# 5. Existing target: no fetch, no create, no description mutation
# ---------------------------------------------------------------------------


def test_existing_target_does_not_fetch_or_create_repo():
    """When ``check_target_repo`` reports the target already exists,
    ``fetch_codeberg_description`` and ``create_github_repo`` must both
    be skipped — the existing repo's description must not be mutated.
    """
    with (
        patch.object(f2gh, "check_target_repo", return_value=_EXISTING_TARGET),
        patch.object(f2gh, "fetch_codeberg_description") as mock_fetch_desc,
        patch.object(f2gh, "create_github_repo") as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=None))  # type: ignore[arg-type]

    mock_fetch_desc.assert_not_called()
    mock_create.assert_not_called()


def test_existing_target_ignores_explicit_description_argument():
    """An explicit ``description`` argument must also be ignored when the
    target already exists — current behavior does not PATCH the
    existing repo's description.
    """
    explicit = "Should be ignored when target exists"

    with (
        patch.object(f2gh, "check_target_repo", return_value=_EXISTING_TARGET),
        patch.object(f2gh, "fetch_codeberg_description") as mock_fetch_desc,
        patch.object(f2gh, "create_github_repo") as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=explicit))  # type: ignore[arg-type]

    mock_fetch_desc.assert_not_called()
    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Dry-run: no create, no description mutation (current observable behavior)
# ---------------------------------------------------------------------------


def test_dry_run_does_not_create_repo_or_mutate_description():
    """``--dry-run`` must not invoke ``create_github_repo`` and must not
    write or persist any description state to ``state.json``. Codeberg
    metadata is still fetched (current observable behavior) but is
    neither posted nor persisted.
    """
    cb_desc = "A description that dry-run must not persist."

    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(
            f2gh, "fetch_codeberg_description", return_value=cb_desc
        ) as mock_fetch_desc,
        patch.object(f2gh, "create_github_repo") as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=None, dry_run=True))  # type: ignore[arg-type]

    mock_fetch_desc.assert_called_once_with("owner/source")
    mock_create.assert_not_called()

    # state.json must not have been created — dry-run never writes it.
    import os

    assert not os.path.exists(f2gh.STATE_FILE), (
        f"dry-run must not create state.json, but it exists at {f2gh.STATE_FILE!r}"
    )


def test_dry_run_does_not_create_repo_when_explicit_description_given():
    """``--dry-run`` with an explicit ``description`` must not invoke
    ``create_github_repo`` and must not consult Codeberg.
    """
    explicit = "Explicit dry-run description"

    with (
        patch.object(f2gh, "check_target_repo", return_value=None),
        patch.object(f2gh, "fetch_codeberg_description") as mock_fetch_desc,
        patch.object(f2gh, "create_github_repo") as mock_create,
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=_NO_ISSUES),
    ):
        f2gh.migrate(**_migrate_kwargs(description=explicit, dry_run=True))  # type: ignore[arg-type]

    mock_fetch_desc.assert_not_called()
    mock_create.assert_not_called()
