"""Codeberg (Forgejo v1) API client.

This module owns the Codeberg/Forgejo side of the migration: repository
metadata, issues, and comments. The :class:`CodebergClient` class wraps
the Forgejo v1 API and translates HTTP failures into a structured error
hierarchy rooted at :class:`CodebergError`.

Design notes
------------

* The client accepts a :class:`~forgejo_to_github.transport.Transport`
  via constructor injection. When ``transport is None``, the client
  instantiates a :class:`~forgejo_to_github.transport.RequestsTransport`
  at construction time (not at module import time).
* Pagination of issues and comments stops at the first empty page. The
  client returns the raw parsed dicts; the orchestrator decides how to
  sort or further filter them.
* Error translation order is fixed by the spec (see ``02-api-clients.md``
  §3.2). 404 → :class:`CodebergNotFoundError`, 401/403 →
  :class:`CodebergAuthError`, 422 → :class:`CodebergValidationError`,
  429 → :class:`CodebergRateLimitError`, 5xx → :class:`CodebergTransportError`.
  Transport-level exceptions (connection refused, DNS failure, timeout)
  are caught and re-raised as :class:`CodebergTransportError`. The token
  must never appear in any error message.
* The Codeberg 403 vs 429 distinction is simple: any 403 maps to
  :class:`CodebergAuthError`. There is no special
  ``X-RateLimit-Remaining: 0`` rule because the Forgejo v1 API uses 429
  for primary rate limits, not the GitHub-style
  ``X-RateLimit-Remaining`` mechanism.
"""

from __future__ import annotations

from typing import Any

from forgejo_to_github.transport import RequestsTransport, Transport

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Issue list page size. Locked by the existing legacy tests; the new
# client uses the same value so end-to-end behavior is unchanged.
_PAGE_LIMIT: int = 50

# User-Agent string. The version is hard-coded because the project is
# not installed as a distribution with version metadata accessible at
# runtime in this layout.
_USER_AGENT: str = "forgejo-to-github/0.2.0"

# ``Accept`` header for JSON responses. Always set.
_ACCEPT_JSON: str = "application/json"


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class CodebergError(Exception):
    """Base class for all Codeberg client errors."""


class CodebergNotFoundError(CodebergError):
    """Raised when Codeberg returns HTTP 404.

    Attributes
    ----------
    issue_number:
        The Codeberg issue number when the failure is on an issue
        endpoint; ``None`` for repository-level 404s.
    url:
        The URL that produced the 404.
    """

    def __init__(self, message: str, url: str, issue_number: int | None = None) -> None:
        super().__init__(message)
        self.url: str = url
        self.issue_number: int | None = issue_number


class CodebergAuthError(CodebergError):
    """Raised when Codeberg returns HTTP 401 or 403."""


class CodebergTransportError(CodebergError):
    """Raised for connection / DNS / timeout failures and HTTP 5xx."""


class CodebergRateLimitError(CodebergError):
    """Raised when Codeberg returns HTTP 429.

    Attributes
    ----------
    retry_after:
        The value of the ``Retry-After`` header (seconds), or ``None``
        when the header is absent.
    """

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after: int | None = retry_after


class CodebergValidationError(CodebergError):
    """Raised when Codeberg returns HTTP 422.

    Attributes
    ----------
    messages:
        Parsed error messages from the response body. The shape of
        each entry is whatever the Forgejo v1 API returned; the client
        does not normalize further.
    """

    def __init__(self, message: str, messages: list[str]) -> None:
        super().__init__(message)
        self.messages: list[str] = messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact(text: str, token: str | None) -> str:
    """Return ``text`` with ``token`` instances replaced.

    A token present only in the message — not at the exception's
    ``__cause__`` chain — is replaced with ``"<redacted>"``. When
    ``token`` is ``None`` or empty, ``text`` is returned unchanged.
    """
    if not token:
        return text
    return text.replace(token, "<redacted>")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CodebergClient:
    """Client for the Codeberg / Forgejo v1 API.

    Parameters
    ----------
    base_url:
        The Codeberg instance base URL, e.g. ``"https://codeberg.org"``.
        The client appends ``/api/v1`` itself.
    owner:
        The repository owner (Codeberg user or org).
    repo:
        The repository name.
    token:
        The Codeberg access token. When ``None``, the client does not
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
        """The ``/api/v1`` URL prefix used for all Codeberg requests."""
        return f"{self._base_url}/api/v1"

    # --- public API ---------------------------------------------------------

    def list_issues(self, state: str = "all") -> list[dict[str, Any]]:
        """List issues, paginating until an empty page is returned.

        The returned list is in the API's natural order (insertion
        order of pages, not sorted by any field). The spec assigns
        sort responsibility to the orchestrator, not the client.
        """
        return self._paginate(
            path=f"/repos/{self._owner}/{self._repo}/issues",
            base_params={"state": state, "type": "issues", "limit": _PAGE_LIMIT},
        )

    def list_comments(self, issue_id: int) -> list[dict[str, Any]]:
        """List comments for a single issue, paginating until empty.

        The ``issue_id`` is passed both as a path segment and as the
        ``issue_id`` query parameter, matching the legacy behavior
        pinned by :file:`tests/test_api_clients.py`.
        """
        return self._paginate(
            path=f"/repos/{self._owner}/{self._repo}/issues/{issue_id}/comments",
            base_params={"issue_id": issue_id},
        )

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        """Fetch a single issue by its Codeberg number."""
        path = f"/repos/{self._owner}/{self._repo}/issues/{issue_number}"
        url = f"{self.api_base}{path}"
        try:
            response = self._transport(
                "GET",
                url,
                headers=self._headers(),
            )
        except Exception as exc:
            raise CodebergTransportError(
                _redact(f"transport error contacting Codeberg: {exc}", self._token)
            ) from exc

        status = getattr(response, "status_code", 0)
        if status == 404:
            raise CodebergNotFoundError(
                f"issue {issue_number} not found on Codeberg",
                url=url,
                issue_number=issue_number,
            )
        if status in (401, 403):
            raise CodebergAuthError(
                _redact(
                    f"authentication failure contacting Codeberg ({status})",
                    self._token,
                )
            )
        if status == 422:
            raise CodebergValidationError(
                "Codeberg rejected the request as invalid",
                messages=_extract_messages(response),
            )
        if status == 429:
            retry_after = _parse_retry_after(response)
            raise CodebergRateLimitError(
                "Codehub rate limit exceeded",
                retry_after=retry_after,
            )
        if 500 <= status < 600:
            raise CodebergTransportError(
                _redact(
                    f"Codeberg server error {status} contacting {url}",
                    self._token,
                )
            )
        if not (200 <= status < 300):
            # Unknown status — treat as transport error to avoid
            # silently dropping an error response.
            raise CodebergTransportError(
                _redact(
                    f"unexpected status {status} contacting {url}",
                    self._token,
                )
            )

        return response.json()

    def get_repository_description(self) -> str:
        """Return the repository's ``description`` field.

        Empty string is returned when the field is missing or
        ``null``. The orchestrator owns the "Migrated from Codeberg"
        fallback; the client does not invent a default.
        """
        path = f"/repos/{self._owner}/{self._repo}"
        url = f"{self.api_base}{path}"
        try:
            response = self._transport(
                "GET",
                url,
                headers=self._headers(),
            )
        except Exception as exc:
            raise CodebergTransportError(
                _redact(f"transport error contacting Codeberg: {exc}", self._token)
            ) from exc

        status = getattr(response, "status_code", 0)
        if status == 404:
            raise CodebergNotFoundError(
                f"repository {self._owner}/{self._repo} not found on Codeberg",
                url=url,
            )
        if status in (401, 403):
            raise CodebergAuthError(
                _redact(
                    f"authentication failure contacting Codeberg ({status})",
                    self._token,
                )
            )
        if status == 422:
            raise CodebergValidationError(
                "Codeberg rejected the repository metadata request",
                messages=_extract_messages(response),
            )
        if status == 429:
            raise CodebergRateLimitError(
                "Codeberg rate limit exceeded",
                retry_after=_parse_retry_after(response),
            )
        if 500 <= status < 600:
            raise CodebergTransportError(
                _redact(
                    f"Codeberg server error {status} contacting {url}",
                    self._token,
                )
            )
        if not (200 <= status < 300):
            raise CodebergTransportError(
                _redact(
                    f"unexpected status {status} contacting {url}",
                    self._token,
                )
            )

        body = response.json()
        description = body.get("description") if isinstance(body, dict) else None
        if description is None:
            return ""
        return str(description)

    # --- internals ----------------------------------------------------------

    def _paginate(self, path: str, base_params: dict[str, Any]) -> list[dict[str, Any]]:
        """Walk pages of a list endpoint until an empty page is returned.

        A 200 response with an empty list terminates the iteration.
        """
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            params = dict(base_params)
            params["page"] = page
            url = f"{self.api_base}{path}"
            try:
                response = self._transport(
                    "GET",
                    url,
                    params=params,
                    headers=self._headers(),
                )
            except Exception as exc:
                raise CodebergTransportError(
                    _redact(
                        f"transport error contacting Codeberg: {exc}",
                        self._token,
                    )
                ) from exc

            status = getattr(response, "status_code", 0)
            if status == 404:
                raise CodebergNotFoundError(
                    f"resource not found at {url}",
                    url=url,
                )
            if status in (401, 403):
                raise CodebergAuthError(
                    _redact(
                        f"authentication failure contacting Codeberg ({status})",
                        self._token,
                    )
                )
            if status == 422:
                raise CodebergValidationError(
                    "Codeberg rejected the request as invalid",
                    messages=_extract_messages(response),
                )
            if status == 429:
                raise CodebergRateLimitError(
                    "Codeberg rate limit exceeded",
                    retry_after=_parse_retry_after(response),
                )
            if 500 <= status < 600:
                raise CodebergTransportError(
                    _redact(
                        f"Codeberg server error {status} contacting {url}",
                        self._token,
                    )
                )
            if not (200 <= status < 300):
                raise CodebergTransportError(
                    _redact(
                        f"unexpected status {status} contacting {url}",
                        self._token,
                    )
                )

            page_items = response.json()
            if not page_items:
                break
            results.extend(page_items)
            page += 1
        return results

    def _headers(self) -> dict[str, str]:
        """Build the default request headers for every Codeberg call."""
        headers: dict[str, str] = {
            "Accept": _ACCEPT_JSON,
            "User-Agent": _USER_AGENT,
        }
        if self._token is not None:
            headers["Authorization"] = f"token {self._token}"
        return headers


# ---------------------------------------------------------------------------
# module-private helpers
# ---------------------------------------------------------------------------


def _extract_messages(response: Any) -> list[str]:
    """Extract a list of human-readable validation messages from a
    422 response body.

    Forgejo returns either ``{"message": "..."}`` or
    ``{"errors": [{"message": "..."}, ...]}``. The client surfaces a
    flat ``list[str]`` regardless of the upstream shape.
    """
    try:
        body = response.json()
    except (ValueError, TypeError):
        return []
    messages: list[str] = []
    if isinstance(body, dict):
        if isinstance(body.get("message"), str):
            messages.append(body["message"])
        errors = body.get("errors")
        if isinstance(errors, list):
            for entry in errors:
                if isinstance(entry, dict):
                    text = entry.get("message")
                    if isinstance(text, str):
                        messages.append(text)
                elif isinstance(entry, str):
                    messages.append(entry)
    return messages


def _parse_retry_after(response: Any) -> int | None:
    """Return the ``Retry-After`` header value as an int, or ``None``."""
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


__all__ = [
    "CodebergAuthError",
    "CodebergClient",
    "CodebergError",
    "CodebergNotFoundError",
    "CodebergRateLimitError",
    "CodebergTransportError",
    "CodebergValidationError",
]
