"""RED-stage test for user-observed contract gap:

When ``migrate()`` runs with ``skip_git=True`` and Codeberg issue fetching
returns HTTP 404 (i.e. the source repository does not exist), the CLI must
terminate gracefully with ``SystemExit`` — not a raw traceback — and report
that the source repository was not found, using a nonzero exit code.
"""

from unittest.mock import patch

import pytest
import requests

import f2gh


def test_migrate_source_404_exits_gracefully(capsys):
    """Codeberg HTTP 404 from fetch_all_codeberg_issues must SystemExit cleanly."""
    fake_response = requests.Response()
    fake_response.status_code = 404
    fake_response.url = "https://codeberg.org/api/v1/repos/owner/missing/issues"

    http_error = requests.HTTPError(
        "404 Client Error: Not Found for url: "
        "https://codeberg.org/api/v1/repos/owner/missing/issues",
        response=fake_response,
    )

    # check_target_repo must return an accessible existing target dict so
    # execution proceeds past Phase 1 to Phase 3 (issue fetch).
    existing_target = {
        "id": 12345,
        "name": "target",
        "full_name": "owner/target",
        "open_issues_count": 0,
    }

    with (
        patch.object(f2gh, "check_target_repo", return_value=existing_target),
        patch.object(f2gh, "fetch_all_codeberg_issues", side_effect=http_error),
        pytest.raises(SystemExit) as exc_info,
    ):
        f2gh.migrate(
            source="owner/missing",
            target="owner/target",
            dry_run=False,
            yes=True,
            skip_git=True,
            public=False,
            description=None,
        )

    # Exit code must be nonzero.
    code = exc_info.value.code
    assert code is not None
    assert code != 0, f"expected nonzero exit code, got {code!r}"

    captured = capsys.readouterr()

    # Combined stdout + stderr: must contain a clear source-not-found message.
    combined = captured.out + captured.err
    assert "owner/missing" in combined, (
        "expected source repo name 'owner/missing' in output, got:\n" + combined
    )
    # At least one of these indicators must appear (case-insensitive).
    combined_lower = combined.lower()
    not_found_signals = (
        "not found",
        "404",
        "source",
    )
    assert any(sig in combined_lower for sig in not_found_signals), (
        "expected a 'not found' / '404' / 'source' indicator in output, got:\n"
        + combined
    )

    # No traceback leaked to stderr.
    assert "Traceback" not in combined, (
        "expected no 'Traceback' in output, but found one:\n" + combined
    )
    assert "Traceback" not in captured.err, (
        "expected no 'Traceback' on stderr, but found one:\n" + captured.err
    )
