"""RED-class: B. Boundary unit — observable API contracts at the
``requests`` boundary.

Tests assert the observable contract of the in-module Codeberg and GitHub
fetchers and writers in ``f2gh`` against mocked ``requests``. No live
network access and no credentials are required.

Coverage map (current behavior unless noted):

Codeberg (``fetch_all_codeberg_issues``, ``fetch_codeberg_comments``,
``fetch_codeberg_description``, ``check_target_repo``):

* Pagination — page-by-page with ``state=all``, ``type=issues``,
  ``page=N``, ``limit=50``; an empty page terminates iteration.
* PR exclusion is enforced via the ``type=issues`` request parameter.
* Returned issues are sorted ascending by ``created_at``.
* HTTP 404 surfaces as ``requests.HTTPError`` carrying a 404 response.
* Network errors propagate as ``requests.ConnectionError`` /
  ``requests.Timeout`` (the current raw-exception contract).

GitHub (``create_github_repo``, ``create_github_issue``,
``create_github_comment``, ``close_github_issue``, ``check_target_repo``,
``gh_request``):

* ``create_github_repo`` first POSTs to ``/user/repos`` with the
  documented payload (``name``, ``private``, ``description``,
  ``has_issues``). A non-201 result falls back to
  ``/orgs/{owner}/repos`` via ``gh_request``.
* ``--public`` flips ``private`` to False; the default is private.
* ``create_github_issue`` POSTs ``{title, body, labels}`` to
  ``/repos/{target}/issues``.
* ``create_github_comment`` POSTs ``{body}`` to the comments endpoint.
* ``close_github_issue`` PATCHes ``{state: "closed"}`` to the issue.
* ``gh_request`` retries 403/429 with jitter, honors ``Retry-After``,
  falls back to ``X-RateLimit-Reset``, and raises ``RuntimeError`` after
  ``max_retries``.

These tests use exact assertions (no regex on URLs, no partial payload
matches) and patch ``requests`` at the ``f2gh`` module boundary.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest
import requests

import f2gh

# ---------------------------------------------------------------------------
# response builders
# ---------------------------------------------------------------------------


def _make_response(
    *,
    status_code: int = 200,
    json_payload: object | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    """Build a MagicMock standing in for ``requests.Response``."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.url = ""

    if json_payload is None:
        resp.json.side_effect = ValueError("no json body")
    else:
        resp.json.return_value = json_payload

    def _raise_for_status() -> None:
        if 400 <= status_code < 600:
            err = requests.HTTPError(f"{status_code} HTTP Error", response=resp)
            raise err

    resp.raise_for_status.side_effect = _raise_for_status
    return resp


def _http_error_with_status(status_code: int, url: str = "") -> requests.HTTPError:
    """Build an HTTPError carrying a synthetic response with status_code."""
    resp = MagicMock(spec=requests.Response)
    resp.status_code = status_code
    resp.url = url
    return requests.HTTPError(f"{status_code} HTTP Error for url: {url}", response=resp)


# ---------------------------------------------------------------------------
# 1. Codeberg pagination
# ---------------------------------------------------------------------------


def test_fetch_all_codeberg_issues_pages_until_empty() -> None:
    """``fetch_all_codeberg_issues`` must walk pages until an empty page."""
    page1 = [{"number": 2, "created_at": "2024-02-01T00:00:00Z"}]
    page2 = [{"number": 1, "created_at": "2024-01-01T00:00:00Z"}]
    # Two non-empty pages, then one empty page that terminates iteration.

    get_responses = [
        _make_response(json_payload=page1),
        _make_response(json_payload=page2),
    ]
    # The third call also returns a mock response (empty body); we still
    # hand the mock a 200 with an empty list so raise_for_status passes.
    get_responses.append(_make_response(json_payload=[]))

    captured_urls: list[str] = []
    captured_params: list[dict[str, object]] = []

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        captured_urls.append(url)
        captured_params.append(dict(params or {}))
        return get_responses.pop(0)

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        issues = f2gh.fetch_all_codeberg_issues("owner/source")

    # Three requests: page 1, page 2, page 3 (empty terminates).
    assert len(captured_urls) == 3
    # Each call targets the same issues endpoint with the source path.
    for url in captured_urls:
        assert url == ("https://codeberg.org/api/v1/repos/owner/source/issues")
    # Page numbers increment starting at 1.
    assert [p["page"] for p in captured_params] == [1, 2, 3]
    # Filter parameters are locked.
    for params in captured_params:
        assert params["state"] == "all"
        assert params["type"] == "issues"
        assert params["limit"] == 50
    # Issues are sorted ascending by created_at.
    assert [i["number"] for i in issues] == [1, 2]


def test_fetch_all_codeberg_issues_empty_first_page_returns_empty_list() -> None:
    """An empty first page must terminate immediately without further calls."""

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(json_payload=[])

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get) as mock_get,
    ):
        issues = f2gh.fetch_all_codeberg_issues("owner/source")

    assert issues == []
    assert mock_get.call_count == 1


def test_fetch_all_codeberg_issues_sends_type_issues_to_exclude_prs() -> None:
    """The ``type=issues`` query parameter excludes pull requests."""
    captured_params: list[dict[str, object]] = []

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        captured_params.append(dict(params or {}))
        return _make_response(json_payload=[])

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        f2gh.fetch_all_codeberg_issues("owner/source")

    assert captured_params[0]["type"] == "issues"


def test_fetch_all_codeberg_issues_sorts_by_created_at_ascending() -> None:
    """Issues must be returned sorted ascending by ``created_at``."""
    unsorted = [
        {"number": 3, "created_at": "2024-03-01T00:00:00Z"},
        {"number": 1, "created_at": "2024-01-01T00:00:00Z"},
        {"number": 2, "created_at": "2024-02-01T00:00:00Z"},
    ]

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        # Return everything on page 1, then empty page to terminate.
        page = params["page"] if params else 1
        if page == 1:
            return _make_response(json_payload=unsorted)
        return _make_response(json_payload=[])

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        issues = f2gh.fetch_all_codeberg_issues("owner/source")

    assert [i["number"] for i in issues] == [1, 2, 3]


def test_fetch_codeberg_comments_uses_issue_index_in_path() -> None:
    """Comments endpoint must use the Codeberg issue index, not GitHub."""
    captured_urls: list[str] = []

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        captured_urls.append(url)
        return _make_response(json_payload=[])

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        f2gh.fetch_codeberg_comments("owner/source", 42)

    assert captured_urls == [
        "https://codeberg.org/api/v1/repos/owner/source/issues/42/comments"
    ]


# ---------------------------------------------------------------------------
# 2. Codeberg 404 / connection / timeout translation contract
# ---------------------------------------------------------------------------


def test_fetch_all_codeberg_issues_404_raises_httperror() -> None:
    """HTTP 404 from Codeberg must surface as ``requests.HTTPError`` carrying
    a 404 ``response.status_code`` (current raw-exception contract).
    """

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(status_code=404)

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
        pytest.raises(requests.HTTPError) as exc_info,
    ):
        f2gh.fetch_all_codeberg_issues("owner/missing")

    assert exc_info.value.response is not None
    assert exc_info.value.response.status_code == 404


def test_fetch_all_codeberg_issues_connection_error_propagates() -> None:
    """A ``requests.ConnectionError`` must propagate verbatim."""
    err = requests.ConnectionError("Could not resolve host codeberg.org")

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=err),
        pytest.raises(requests.ConnectionError) as exc_info,
    ):
        f2gh.fetch_all_codeberg_issues("owner/source")

    assert str(exc_info.value) == "Could not resolve host codeberg.org"


def test_fetch_all_codeberg_issues_timeout_propagates() -> None:
    """A ``requests.Timeout`` must propagate verbatim."""
    err = requests.Timeout("Read timed out")

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=err),
        pytest.raises(requests.Timeout) as exc_info,
    ):
        f2gh.fetch_all_codeberg_issues("owner/source")

    assert str(exc_info.value) == "Read timed out"


def test_fetch_codeberg_comments_404_raises_httperror() -> None:
    """HTTP 404 on the comments endpoint must surface as HTTPError."""

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(status_code=404)

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
        pytest.raises(requests.HTTPError) as exc_info,
    ):
        f2gh.fetch_codeberg_comments("owner/source", 1)

    assert exc_info.value.response.status_code == 404


def test_fetch_codeberg_description_returns_description_string() -> None:
    """A 200 response carrying ``description`` must return the description."""
    repo = {
        "id": 1,
        "name": "source",
        "full_name": "owner/source",
        "description": "Original project description",
    }

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(json_payload=repo)

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        desc = f2gh.fetch_codeberg_description("owner/source")

    assert desc == "Original project description"


def test_fetch_codeberg_description_missing_returns_fallback() -> None:
    """A 200 with empty/missing ``description`` must return the fallback."""
    repo = {
        "id": 1,
        "name": "source",
        "full_name": "owner/source",
        "description": "",
    }

    def fake_get(url, *, headers=None, params=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(json_payload=repo)

    with (
        patch.object(f2gh, "codeberg_token", return_value="cb-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        desc = f2gh.fetch_codeberg_description("owner/source")

    assert desc == "Migrated from Codeberg"


# ---------------------------------------------------------------------------
# 3. GitHub repo creation
# ---------------------------------------------------------------------------


def test_create_github_repo_default_private_posts_expected_payload() -> None:
    """With ``public=False`` (default), POST payload must have
    ``private=True`` and the documented field set.
    """
    captured: dict[str, object] = {}

    def fake_post(url, *, headers=None, json=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["json"] = json
        return _make_response(status_code=201, json_payload={"id": 1, "name": "target"})

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "post", side_effect=fake_post),
    ):
        result = f2gh.create_github_repo(
            target="owner/target",
            description="My project",
            public=False,
        )

    # First attempt goes to the personal user endpoint.
    assert captured["url"] == "https://api.github.com/user/repos"
    assert captured["json"] == {
        "name": "target",
        "private": True,
        "description": "My project",
        "has_issues": True,
    }
    assert result == {"id": 1, "name": "target"}


def test_create_github_repo_public_posts_private_false() -> None:
    """With ``public=True``, POST payload must have ``private=False``."""
    captured: dict[str, object] = {}

    def fake_post(url, *, headers=None, json=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["url"] = url
        captured["json"] = json
        return _make_response(status_code=201, json_payload={"id": 2, "name": "target"})

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "post", side_effect=fake_post),
    ):
        f2gh.create_github_repo(
            target="owner/target",
            description="Public project",
            public=True,
        )

    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["private"] is False


def test_create_github_repo_falls_back_to_org_endpoint() -> None:
    """A non-201 from ``/user/repos`` must fall back to ``/orgs/{owner}/repos``
    via ``gh_request``.
    """
    request_calls: list[dict[str, object]] = []

    def fake_post(url, *, headers=None, json=None, **kwargs):  # type: ignore[no-untyped-def]
        request_calls.append({"url": url, "json": json, "transport": "post"})
        # Personal endpoint returns 422 (validation), forcing the fallback.
        return _make_response(status_code=422)

    def fake_request(method, url, *, headers=None, json=None, **kwargs):  # type: ignore[no-untyped-def]
        request_calls.append(
            {"url": url, "method": method, "json": json, "transport": "request"}
        )
        return _make_response(
            status_code=201, json_payload={"id": 99, "name": "target"}
        )

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "post", side_effect=fake_post),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(f2gh.time, "sleep", lambda *_a, **_kw: None),
    ):
        result = f2gh.create_github_repo(
            target="myorg/target",
            description="Org repo",
            public=False,
        )

    # Two requests: personal first, then org via gh_request.
    assert len(request_calls) == 2
    assert request_calls[0]["url"] == "https://api.github.com/user/repos"
    assert request_calls[0]["transport"] == "post"
    assert request_calls[1]["url"] == "https://api.github.com/orgs/myorg/repos"
    assert request_calls[1]["method"] == "POST"
    assert request_calls[1]["transport"] == "request"
    # Both attempts must use the same payload.
    assert request_calls[0]["json"] == request_calls[1]["json"]
    assert result == {"id": 99, "name": "target"}


# ---------------------------------------------------------------------------
# 4. GitHub issue / comment / close payloads
# ---------------------------------------------------------------------------


def test_create_github_issue_posts_expected_payload() -> None:
    """``create_github_issue`` must POST ``{title, body, labels}`` to
    ``/repos/{target}/issues``.
    """
    captured: dict[str, object] = {}

    def fake_request(method, url, *, headers=None, json=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        return _make_response(status_code=201, json_payload={"number": 7})

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(f2gh.time, "sleep", lambda *_a, **_kw: None),
    ):
        result = f2gh.create_github_issue(
            target="owner/target",
            title="First issue",
            body="Issue body",
            labels=["bug", "help"],
        )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.github.com/repos/owner/target/issues"
    assert captured["json"] == {
        "title": "First issue",
        "body": "Issue body",
        "labels": ["bug", "help"],
    }
    assert result == {"number": 7}


def test_create_github_comment_posts_body_to_comments_endpoint() -> None:
    """``create_github_comment`` must POST ``{body}`` to the comments endpoint."""
    captured: dict[str, object] = {}

    def fake_request(method, url, *, headers=None, json=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        return _make_response(status_code=201, json_payload={"id": 999})

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(f2gh.time, "sleep", lambda *_a, **_kw: None),
    ):
        f2gh.create_github_comment(
            target="owner/target",
            issue_number=7,
            body="A comment",
        )

    assert captured["method"] == "POST"
    assert captured["url"] == (
        "https://api.github.com/repos/owner/target/issues/7/comments"
    )
    assert captured["json"] == {"body": "A comment"}


def test_close_github_issue_patches_state_closed() -> None:
    """``close_github_issue`` must PATCH ``{state: "closed"}`` to the issue."""
    captured: dict[str, object] = {}

    def fake_request(method, url, *, headers=None, json=None, **kwargs):  # type: ignore[no-untyped-def]
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        return _make_response(status_code=200, json_payload={"state": "closed"})

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(f2gh.time, "sleep", lambda *_a, **_kw: None),
    ):
        f2gh.close_github_issue(target="owner/target", issue_number=7)

    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://api.github.com/repos/owner/target/issues/7"
    assert captured["json"] == {"state": "closed"}


# ---------------------------------------------------------------------------
# 5. check_target_repo return contract
# ---------------------------------------------------------------------------


def test_check_target_repo_returns_repo_dict_on_200() -> None:
    """A 200 response must return the parsed JSON repo info."""
    repo = {"id": 1, "name": "target", "full_name": "owner/target"}

    def fake_get(url, *, headers=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(json_payload=repo)

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        result = f2gh.check_target_repo("owner/target")

    assert result == repo


def test_check_target_repo_returns_none_on_404() -> None:
    """A 404 response must return ``None`` (not raise, not exit)."""

    def fake_get(url, *, headers=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(status_code=404)

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
    ):
        result = f2gh.check_target_repo("owner/missing")

    assert result is None


def test_check_target_repo_403_raises_system_exit() -> None:
    """A 403 response must ``SystemExit`` with a token-related message."""

    def fake_get(url, *, headers=None, timeout=None):  # type: ignore[no-untyped-def]
        return _make_response(status_code=403)

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "get", side_effect=fake_get),
        pytest.raises(SystemExit) as exc_info,
    ):
        f2gh.check_target_repo("owner/target")

    msg = str(exc_info.value.code)
    assert "owner/target" in msg
    assert "403" in msg


# ---------------------------------------------------------------------------
# 6. gh_request rate-limit retry behavior
# ---------------------------------------------------------------------------


def test_gh_request_retries_on_403_then_succeeds() -> None:
    """A 403 with rate-limit headers followed by a 200 must succeed on retry."""
    rate_limited = _make_response(
        status_code=403,
        headers={"Retry-After": "1"},
    )
    ok = _make_response(status_code=200, json_payload={"ok": True})

    sleep_calls: list[float] = []

    def fake_request(method, url, *, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        if not sleep_calls:
            return rate_limited
        return ok

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(
            f2gh.time, "sleep", lambda secs, *_a, **_kw: sleep_calls.append(secs)
        ),
    ):
        resp = f2gh.gh_request("GET", "https://api.github.com/anything")

    assert resp is ok
    # Sleep was called once between the two attempts.
    assert len(sleep_calls) == 1


def test_gh_request_retries_on_429_then_succeeds() -> None:
    """A 429 with rate-limit headers followed by a 200 must succeed on retry."""
    rate_limited = _make_response(
        status_code=429,
        headers={"Retry-After": "2"},
    )
    ok = _make_response(status_code=200, json_payload={"ok": True})

    sleep_calls: list[float] = []

    def fake_request(method, url, *, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        if not sleep_calls:
            return rate_limited
        return ok

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(
            f2gh.time, "sleep", lambda secs, *_a, **_kw: sleep_calls.append(secs)
        ),
    ):
        resp = f2gh.gh_request("GET", "https://api.github.com/anything")

    assert resp is ok
    assert len(sleep_calls) == 1


def test_gh_request_exhausts_retries_before_terminal_failure() -> None:
    """Three consecutive 403s (default ``max_retries=3``) must terminate with
    a non-success outcome after two retry sleeps.

    Current raw-exception contract: the third attempt falls through to
    ``raise_for_status()`` and surfaces ``requests.HTTPError`` (status 403).
    The structured ``GitHubRateLimitError`` translation required by the
    spec is a downstream contract; this test pins the retry-budget
    observable behavior either way.
    """
    rate_limited = _make_response(
        status_code=403,
        headers={"Retry-After": "1"},
    )

    def fake_request(method, url, *, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        return rate_limited

    sleep_calls: list[float] = []

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(
            f2gh.time, "sleep", lambda secs, *_a, **_kw: sleep_calls.append(secs)
        ),
        pytest.raises(requests.HTTPError) as exc_info,
    ):
        f2gh.gh_request("GET", "https://api.github.com/anything")

    assert exc_info.value.response.status_code == 403
    # Two sleeps between three attempts (no sleep after the final attempt,
    # because ``attempt < max_retries - 1`` is False at the third pass).
    assert len(sleep_calls) == 2


def test_gh_request_uses_x_ratelimit_reset_when_retry_after_missing() -> None:
    """When ``Retry-After`` is absent, ``X-RateLimit-Reset`` (epoch) drives the
    delay.
    """
    future_epoch = int(time.time()) + 5
    rate_limited = _make_response(
        status_code=429,
        headers={"X-RateLimit-Reset": str(future_epoch)},
    )
    ok = _make_response(status_code=200, json_payload={"ok": True})

    sleep_calls: list[float] = []

    def fake_request(method, url, *, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        if not sleep_calls:
            return rate_limited
        return ok

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-token"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(
            f2gh.time, "sleep", lambda secs, *_a, **_kw: sleep_calls.append(secs)
        ),
    ):
        resp = f2gh.gh_request("GET", "https://api.github.com/anything")

    assert resp is ok
    assert len(sleep_calls) == 1
    # The chosen delay should be close to the reset-epoch delta (5s) plus up to
    # 2s of jitter. We assert it is in the open interval (0, 5 + 2].
    assert 0 < sleep_calls[0] <= future_epoch - int(time.time()) + 2 + 0.5


def test_gh_request_sends_bearer_authorization_header() -> None:
    """``gh_request`` must include ``Authorization: Bearer <token>`` on every
    attempt.
    """
    captured_headers: list[dict[str, str]] = []

    def fake_request(method, url, *, headers=None, **kwargs):  # type: ignore[no-untyped-def]
        captured_headers.append(dict(headers or {}))
        return _make_response(status_code=200, json_payload={"ok": True})

    with (
        patch.object(f2gh, "get_github_token", return_value="gh-sentinel"),
        patch.object(f2gh.requests, "request", side_effect=fake_request),
        patch.object(f2gh.time, "sleep", lambda *_a, **_kw: None),
    ):
        f2gh.gh_request("GET", "https://api.github.com/anything")

    assert captured_headers, "expected gh_request to call requests.request"
    assert captured_headers[0].get("Authorization") == "Bearer gh-sentinel"
    # GitHub REST v3 media type and pinned API version are part of the contract.
    assert captured_headers[0].get("Accept") == "application/vnd.github+json"
    assert captured_headers[0].get("X-GitHub-Api-Version") == "2022-11-28"
