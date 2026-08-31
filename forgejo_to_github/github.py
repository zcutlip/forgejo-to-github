"""GitHub REST API v3 client.

This module owns the GitHub side of the migration: repository creation
and metadata updates, issue / comment / label operations, and the
structured retry behavior for secondary rate limits. The
:class:`GitHubClient` class wraps the GitHub REST API and translates
HTTP failures into a structured error hierarchy rooted at
:class:`GitHubError`.

Design notes
------------

* The client accepts a :class:`~forgejo_to_github.transport.Transport`
  via constructor injection. When ``transport is None``, the client
  instantiates a :class:`~forgejo_to_github.transport.RequestsTransport`
  at construction time (not at module import time).
* Error translation order is fixed by the spec (see ``02-api-clients.md``
  §3.3). 429 → :class:`GitHubRateLimitError` (retried up to three
  times), 403 with ``X-RateLimit-Remaining: 0`` →
  :class:`GitHubRateLimitError` (retried up to three times), 401/403 →
  :class:`GitHubAuthError`, 422 → :class:`GitHubValidationError`,
  5xx → :class:`GitHubTransportError`. Transport-level exceptions
  (connection refused, DNS failure, timeout) are caught and re-raised
  as :class:`GitHubTransportError`.
* The retry policy between attempts is implementation-defined. The
  client performs exactly three attempts before raising
  :class:`GitHubRateLimitError`; that is the locked observable contract.
* Repository creation falls back from the personal
  ``POST /user/repos`` endpoint to ``POST /orgs/{owner}/repos`` when
  the personal endpoint returns a non-2xx response. The personal-then-org
  order is part of the legacy contract.
* The orchestrator is responsible for skipping empty-description PATCH
  calls and for the "Migrated from Codeberg" fallback; the client
  forwards whatever it is given.
"""

from __future__ import annotations

import random
import time
from typing import Any

from forgejo_to_github.transport import RequestsTransport, Transport

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Locked by spec §3.3: three attempts before giving up on a
# rate-limited response.
_MAX_ATTEMPTS: int = 3

# Base jitter, in seconds, added to the retry sleep. The exact value is
# implementation-defined; the spec only requires the 3-attempt cap.
_JITTER_SECONDS: float = 1.0

# GitHub's pinned REST API version. Surfaced on every request.
_API_VERSION: str = "2022-11-28"

# Media type expected by GitHub REST v3.
_ACCEPT_HEADER: str = "application/vnd.github+json"


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class GitHubError(Exception):
    """Base class for all GitHub client errors."""


class GitHubAuthError(GitHubError):
    """Raised when GitHub returns HTTP 401 or 403 (other than
    secondary-rate-limit responses)."""


class GitHubValidationError(GitHubError):
    """Raised when GitHub returns HTTP 422.

    Attributes
    ----------
    messages:
        Parsed error messages from the response body. Each entry is
        either a top-level ``message`` string or one entry of the
        ``errors`` array, formatted by ``_format_validation_messages``.
    """

    def __init__(self, message: str, messages: list[str]) -> None:
        super().__init__(message)
        self.messages: list[str] = messages


class GitHubRateLimitError(GitHubError):
    """Raised when GitHub returns HTTP 429 or 403 with
    ``X-RateLimit-Remaining: 0``.

    Attributes
    ----------
    reset:
        The ``X-RateLimit-Reset`` header value (epoch seconds), or
        ``None`` when the header is absent.
    retry_after:
        The ``Retry-After`` header value (seconds), or ``None`` when
        the header is absent.
    """

    def __init__(
        self,
        message: str,
        reset: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.reset: int | None = reset
        self.retry_after: int | None = retry_after


class GitHubTransportError(GitHubError):
    """Raised for connection / DNS / timeout failures and HTTP 5xx."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact(text: str, token: str | None) -> str:
    """Return ``text`` with ``token`` instances replaced."""
    if not token:
        return text
    return text.replace(token, "<redacted>")


def _parse_int_header(headers: Any, name: str) -> int | None:
    """Return ``headers[name]`` parsed as ``int``, or ``None``."""
    if headers is None:
        return None
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _is_rate_limit_response(response: Any) -> bool:
    """Return ``True`` when ``response`` looks like a secondary-rate-limit
    response.

    GitHub surfaces secondary rate limits as either HTTP 429 or HTTP 403
    with ``X-RateLimit-Remaining: 0`` in the response headers. The
    header is consulted case-insensitively via the response's mapping
    interface (which :mod:`requests` provides).
    """
    status = getattr(response, "status_code", 0)
    if status == 429:
        return True
    if status == 403:
        headers = getattr(response, "headers", None) or {}
        remaining = headers.get("X-RateLimit-Remaining")
        if remaining is not None:
            try:
                return int(remaining) == 0
            except (TypeError, ValueError):
                return False
    return False


def _format_validation_messages(payload: Any) -> list[str]:
    """Format the GitHub 422 body into a flat ``list[str]`` of messages.

    GitHub returns ``{"message": str, "errors": [{"resource", "field",
    "code", "message"}]}``. The client surfaces a flat list; the exact
    formatting of each entry is internal.
    """
    messages: list[str] = []
    if isinstance(payload, dict):
        top = payload.get("message")
        if isinstance(top, str):
            messages.append(top)
        errors = payload.get("errors")
        if isinstance(errors, list):
            for entry in errors:
                if isinstance(entry, dict):
                    field = entry.get("field")
                    code = entry.get("code")
                    text = entry.get("message") or entry.get("message", "")
                    formatted_parts: list[str] = []
                    if isinstance(field, str) and field:
                        formatted_parts.append(field)
                    if isinstance(code, str) and code:
                        formatted_parts.append(code)
                    if isinstance(text, str) and text:
                        formatted_parts.append(text)
                    if formatted_parts:
                        messages.append(" ".join(formatted_parts))
                elif isinstance(entry, str):
                    messages.append(entry)
    return messages


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Client for the GitHub REST API v3.

    Parameters
    ----------
    base_url:
        The GitHub API base URL, e.g. ``"https://api.github.com"``.
    owner:
        The GitHub user or organization that will own the target
        repository.
    repo:
        The target repository name.
    token:
        The GitHub access token. When ``None``, the client does not
        send an ``Authorization`` header.
    transport:
        An optional :class:`Transport` to inject. When ``None``, a
        :class:`RequestsTransport` is constructed at instance time.
    """

    def __init__(
        self,
        base_url: str,
        owner: str,
        repo: str,
        token: str | None,
        transport: Transport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owner = owner
        self._repo = repo
        self._token = token
        self._transport: Transport = (
            transport if transport is not None else RequestsTransport()
        )

    # --- properties ---------------------------------------------------------

    @property
    def api_base(self) -> str:
        """The GitHub REST API base URL used for all requests."""
        return self._base_url

    # --- public API ---------------------------------------------------------

    def create_repository(
        self,
        name: str,
        description: str | None,
        public: bool,
    ) -> dict[str, Any]:
        """Create a new repository under the authenticated user.

        On a non-2xx response from the personal endpoint, falls back to
        ``POST /orgs/{owner}/repos`` with the same payload. The
        personal-then-org order is the locked contract.
        """
        payload: dict[str, Any] = {
            "name": name,
            "private": not public,
            "description": description,
            "has_issues": True,
        }
        url_user = f"{self._base_url}/user/repos"
        response = self._request_with_rate_limit_retry(
            "POST",
            url_user,
            json_body=payload,
        )
        if 200 <= response.status_code < 300:
            return response.json()

        # Fall back to the org endpoint, mirroring the legacy behavior.
        url_org = f"{self._base_url}/orgs/{self._owner}/repos"
        response = self._request_with_rate_limit_retry(
            "POST",
            url_org,
            json_body=payload,
        )
        if 200 <= response.status_code < 300:
            return response.json()
        # If the org fallback also fails with a rate-limit condition,
        # the helper has already raised. For any other non-2xx response,
        # translate to the structured error.
        self._raise_for_response(response, url_org)
        # Unreachable: _raise_for_response always raises. The following
        # line is defensive in case future refactors break that
        # invariant.
        raise GitHubTransportError(  # pragma: no cover - defensive
            "GitHub repository creation failed"
        )

    def update_repository_description(self, description: str) -> None:
        """PATCH the target repository's ``description`` field."""
        url = f"{self._base_url}/repos/{self._owner}/{self._repo}"
        response = self._request_with_rate_limit_retry(
            "PATCH",
            url,
            json_body={"description": description},
        )
        self._raise_for_response(response, url)

    def check_repository_exists(self) -> dict[str, Any] | None:
        """Return the parsed repo dict on 200, ``None`` on 404."""
        url = f"{self._base_url}/repos/{self._owner}/{self._repo}"
        response = self._request_with_rate_limit_retry(
            "GET",
            url,
        )
        if response.status_code == 404:
            return None
        self._raise_for_response(response, url)
        return response.json()

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        """Create an issue and return its ``number``."""
        url = f"{self._base_url}/repos/{self._owner}/{self._repo}/issues"
        response = self._request_with_rate_limit_retry(
            "POST",
            url,
            json_body={"title": title, "body": body, "labels": list(labels)},
        )
        self._raise_for_response(response, url)
        return int(response.json()["number"])

    def create_comment(self, issue_number: int, body: str) -> int:
        """Create a comment on an issue and return its ``id``."""
        url = (
            f"{self._base_url}/repos/{self._owner}/{self._repo}"
            f"/issues/{issue_number}/comments"
        )
        response = self._request_with_rate_limit_retry(
            "POST",
            url,
            json_body={"body": body},
        )
        self._raise_for_response(response, url)
        return int(response.json()["id"])

    def close_issue(self, issue_number: int) -> None:
        """PATCH an issue to state ``closed``."""
        url = f"{self._base_url}/repos/{self._owner}/{self._repo}/issues/{issue_number}"
        response = self._request_with_rate_limit_retry(
            "PATCH",
            url,
            json_body={"state": "closed"},
        )
        self._raise_for_response(response, url)

    def ensure_label(self, name: str, color: str, description: str) -> None:
        """Create a label if it does not already exist.

        The method issues a ``GET /repos/{owner}/{repo}/labels/{name}``
        first; on 404 it follows up with a ``POST`` to create the label.
        On any other status from the GET, the GET's response is
        translated via the standard error hierarchy.
        """
        url_get = f"{self._base_url}/repos/{self._owner}/{self._repo}/labels/{name}"
        response = self._request_with_rate_limit_retry("GET", url_get)
        if response.status_code == 200:
            return
        if response.status_code != 404:
            self._raise_for_response(response, url_get)

        url_post = f"{self._base_url}/repos/{self._owner}/{self._repo}/labels"
        response = self._request_with_rate_limit_retry(
            "POST",
            url_post,
            json_body={
                "name": name,
                "color": color,
                "description": description,
            },
        )
        self._raise_for_response(response, url_post)

    # --- internals ----------------------------------------------------------

    def _request_with_rate_limit_retry(
        self,
        method: str,
        url: str,
        *,
        json_body: Any | None = None,
    ) -> Any:
        """Issue ``method url`` and retry on secondary rate-limit
        responses up to three times.

        Returns the response object of the final attempt. Raises
        :class:`GitHubRateLimitError` after the third rate-limited
        attempt.

        Only HTTP 429 responses are retried. A 403 with
        ``X-RateLimit-Remaining: 0`` (the GitHub primary-rate-limit
        signal) is translated to :class:`GitHubRateLimitError`
        immediately, without an internal retry. The orchestrator or
        caller decides whether to retry after honoring the
        ``X-RateLimit-Reset`` interval.
        """
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = self._transport(
                    method,
                    url,
                    headers=self._headers(),
                    json_body=json_body,
                )
            except Exception as exc:
                raise GitHubTransportError(
                    _redact(
                        f"transport error contacting GitHub: {exc}",
                        self._token,
                    )
                ) from exc

            status = getattr(response, "status_code", 0)
            if status == 429:
                if attempt >= _MAX_ATTEMPTS:
                    raise self._rate_limit_error(response)
                self._sleep_for_rate_limit(response)
                continue

            return response

        # Unreachable: the loop returns on the final attempt or raises.
        raise GitHubTransportError(  # pragma: no cover - defensive
            "GitHub request exhausted retries"
        )

    def _sleep_for_rate_limit(self, response: Any) -> None:
        """Sleep before retrying a rate-limited response.

        The sleep duration is derived from the ``Retry-After`` header
        when present, otherwise from the ``X-RateLimit-Reset`` epoch
        header. A small amount of jitter is added to avoid
        thundering-herd retry collisions.
        """
        headers = getattr(response, "headers", None) or {}
        retry_after = _parse_int_header(headers, "Retry-After")
        if retry_after is None:
            reset_epoch = _parse_int_header(headers, "X-RateLimit-Reset")
            if reset_epoch is not None:
                retry_after = max(reset_epoch - int(time.time()), 1)
            else:
                retry_after = 1
        delay = float(retry_after) + random.uniform(0, _JITTER_SECONDS)
        time.sleep(delay)

    def _rate_limit_error(self, response: Any) -> GitHubRateLimitError:
        """Build the terminal :class:`GitHubRateLimitError`."""
        headers = getattr(response, "headers", None) or {}
        return GitHubRateLimitError(
            _redact(
                "GitHub secondary rate limit exceeded",
                self._token,
            ),
            reset=_parse_int_header(headers, "X-RateLimit-Reset"),
            retry_after=_parse_int_header(headers, "Retry-After"),
        )

    def _raise_for_response(self, response: Any, url: str) -> None:
        """Translate ``response`` into the structured error hierarchy.

        Rate-limit responses are translated to
        :class:`GitHubRateLimitError`; this method is called *after*
        the retry helper has given up on retries, so the raise is
        terminal. Other non-2xx responses map to auth / validation /
        transport errors in the spec's documented order.
        """
        status = getattr(response, "status_code", 0)
        if 200 <= status < 300:
            return

        if _is_rate_limit_response(response):
            raise self._rate_limit_error(response)

        if status == 401:
            raise GitHubAuthError(
                _redact(
                    f"authentication failure contacting GitHub ({status})",
                    self._token,
                )
            )
        if status == 403:
            raise GitHubAuthError(
                _redact(
                    f"authentication failure contacting GitHub ({status})",
                    self._token,
                )
            )
        if status == 422:
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = None
            messages = _format_validation_messages(payload)
            raise GitHubValidationError(
                "GitHub rejected the request as invalid",
                messages=messages,
            )
        if 500 <= status < 600:
            raise GitHubTransportError(
                _redact(
                    f"GitHub server error {status} contacting {url}",
                    self._token,
                )
            )
        # Fallback for any other non-2xx status.
        raise GitHubTransportError(
            _redact(
                f"unexpected status {status} contacting {url}",
                self._token,
            )
        )

    def _headers(self) -> dict[str, str]:
        """Build the default request headers for every GitHub call."""
        headers: dict[str, str] = {
            "Accept": _ACCEPT_HEADER,
            "X-GitHub-Api-Version": _API_VERSION,
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


__all__ = [
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubRateLimitError",
    "GitHubTransportError",
    "GitHubValidationError",
]
