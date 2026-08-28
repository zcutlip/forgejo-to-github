"""RED-stage tests for migration failure handling, reporting, and resume.

These characterize observable contracts of ``f2gh.migrate()`` Phase 2 (git
mirror) and Phase 3 (issue migration):

1. A Git *push* failure is non-fatal: migration continues into issue fetch and
   the final report says Git FAILED without claiming ``All issues migrated.``
2. A Git *clone* failure is terminal: ``migrate()`` re-raises ``SystemExit``
   and issue fetching never happens.
3. A per-issue ``HTTPError`` is accumulated and later issues still migrate; the
   final report lists the failed Codeberg issue.
4. Successful issues are checkpointed to ``state.json`` and a second run
   filters out already-migrated issues.

All network and subprocess interactions are mocked; no live APIs are used.
"""

from unittest.mock import patch

import pytest
import requests

import f2gh

EXISTING_TARGET: dict[str, object] = {
    "id": 12345,
    "name": "target",
    "full_name": "owner/target",
    "open_issues_count": 0,
}


def _issue(number: int, title: str, state: str = "open") -> dict:
    """Build a deterministic Codeberg issue payload."""
    return {
        "number": number,
        "title": title,
        "state": state,
        "body": f"body for {number}",
        "created_at": f"2024-01-0{number}T00:00:00Z",
        "user": {"username": "cbuser"},
        "labels": [],
    }


def _http_error(message: str, status: int = 500) -> requests.HTTPError:
    """Build an HTTPError carrying a response with the given status."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(message, response=response)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Never touch the repository's real state.json."""
    monkeypatch.setattr(f2gh, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(f2gh.time, "sleep", lambda *_args, **_kwargs: None)
    return tmp_path / "state.json"


def test_git_push_failure_is_non_fatal_and_reported(capsys):
    """A push failure must not abort migration, and must be reported truthfully."""
    push_failure = SystemExit("ERROR: Git push failed: ! [remote rejected] main")

    with (
        patch.object(f2gh, "check_target_repo", return_value=EXISTING_TARGET),
        patch.object(f2gh, "mirror_git_repo", side_effect=push_failure) as mock_mirror,
        patch.object(
            f2gh, "fetch_all_codeberg_issues", return_value=[]
        ) as mock_fetch_issues,
    ):
        f2gh.migrate(
            source="owner/source",
            target="owner/target",
            dry_run=False,
            yes=True,
            skip_git=False,
            public=False,
            description=None,
        )

    assert mock_mirror.call_count == 1
    # Migration must have proceeded past Phase 2 into Phase 3.
    mock_fetch_issues.assert_called_once_with("owner/source")

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "Git: FAILED" in combined, (
        "expected the final report to state 'Git: FAILED', got:\n" + combined
    )
    assert "All issues migrated." not in combined, (
        "must not claim complete success when git push failed, got:\n" + combined
    )


def test_clone_failure_is_terminal_and_skips_issue_fetch():
    """A clone failure must abort before any issue fetching occurs."""
    clone_failure = SystemExit(
        "ERROR: Clone failed: fatal: repository not found\n  Verify the source repository and retry."
    )

    with (
        patch.object(f2gh, "check_target_repo", return_value=EXISTING_TARGET),
        patch.object(f2gh, "mirror_git_repo", side_effect=clone_failure),
        patch.object(f2gh, "fetch_all_codeberg_issues") as mock_fetch_issues,
        pytest.raises(SystemExit) as exc_info,
    ):
        f2gh.migrate(
            source="owner/source",
            target="owner/target",
            dry_run=False,
            yes=True,
            skip_git=False,
            public=False,
            description=None,
        )

    assert "Clone failed" in str(exc_info.value.code)
    mock_fetch_issues.assert_not_called()


def test_issue_failure_is_accumulated_and_later_issues_continue(capsys):
    """One failing issue must not stop the next one, and must be reported."""
    issues = [_issue(1, "first issue"), _issue(2, "second issue")]

    create_results: list[object] = [
        _http_error("500 Server Error: Internal Server Error"),
        {"number": 4242},
    ]

    def fake_create_issue(target, title, body, labels):
        result = create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    with (
        patch.object(f2gh, "check_target_repo", return_value=EXISTING_TARGET),
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=issues),
        patch.object(f2gh, "fetch_codeberg_comments", return_value=[]),
        patch.object(
            f2gh, "create_github_issue", side_effect=fake_create_issue
        ) as mock_create,
        patch.object(f2gh, "create_github_comment") as mock_comment,
        patch.object(f2gh, "close_github_issue"),
    ):
        f2gh.migrate(
            source="owner/source",
            target="owner/target",
            dry_run=False,
            yes=True,
            skip_git=True,
            public=False,
            description=None,
        )

    # Both issues were attempted; the second one succeeded.
    assert mock_create.call_count == 2
    assert mock_create.call_args_list[1].args[1] == "second issue"
    mock_comment.assert_not_called()

    state = f2gh.load_state("owner/source", "owner/target")
    assert state["migrated"] == {2: 4242}, (
        "only the successful issue should be checkpointed, got: " + repr(state)
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "Failed issues" in combined, (
        "expected a 'Failed issues' section in the report, got:\n" + combined
    )
    assert "CB #" in combined, (
        "expected the failed issue to be labelled with 'CB #', got:\n" + combined
    )
    assert "first issue" in combined, (
        "expected the failed issue title in the report, got:\n" + combined
    )
    assert "All issues migrated." not in combined, (
        "must not claim complete success when an issue failed, got:\n" + combined
    )


def test_successful_issues_are_checkpointed_and_resume_filters_them(capsys):
    """A second run must skip already-migrated issues and only process new ones."""
    first_run_issues = [_issue(1, "first issue")]
    second_run_issues = [_issue(1, "first issue"), _issue(2, "second issue")]

    numbers = iter([101, 102])

    def fake_create_issue(target, title, body, labels):
        return {"number": next(numbers)}

    migrate_kwargs = {
        "source": "owner/source",
        "target": "owner/target",
        "dry_run": False,
        "yes": True,
        "skip_git": True,
        "public": False,
        "description": None,
    }

    with (
        patch.object(f2gh, "check_target_repo", return_value=EXISTING_TARGET),
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=first_run_issues),
        patch.object(f2gh, "fetch_codeberg_comments", return_value=[]),
        patch.object(f2gh, "create_github_issue", side_effect=fake_create_issue),
        patch.object(f2gh, "close_github_issue"),
    ):
        f2gh.migrate(**migrate_kwargs)  # type: ignore[arg-type]

    assert f2gh.load_state("owner/source", "owner/target")["migrated"] == {1: 101}

    capsys.readouterr()  # discard first-run output

    with (
        patch.object(f2gh, "check_target_repo", return_value=EXISTING_TARGET),
        patch.object(f2gh, "fetch_all_codeberg_issues", return_value=second_run_issues),
        patch.object(f2gh, "fetch_codeberg_comments", return_value=[]),
        patch.object(
            f2gh, "create_github_issue", side_effect=fake_create_issue
        ) as mock_create,
        patch.object(f2gh, "close_github_issue"),
    ):
        f2gh.migrate(**migrate_kwargs)  # type: ignore[arg-type]

    # Only the previously unrecorded issue is processed on resume.
    assert mock_create.call_count == 1
    assert mock_create.call_args_list[0].args[1] == "second issue"

    assert f2gh.load_state("owner/source", "owner/target")["migrated"] == {
        1: 101,
        2: 102,
    }

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "already migrated" in combined, (
        "expected the resume run to report skipped issues, got:\n" + combined
    )
