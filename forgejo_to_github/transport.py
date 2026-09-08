"""HTTP transport abstraction for the Codeberg and GitHub API clients.

This module defines the :class:`Transport` Protocol and a
:class:`RequestsTransport` default adapter built on top of
``requests.Session``. The clients depend on the Protocol; tests inject
their own fakes that satisfy the same call signature.

Design notes
------------

* The Protocol is structural (duck-typed). A class is a valid transport
  when it implements ``__call__`` with the documented keyword-only
  parameters.
* The default adapter imports :mod:`requests` lazily inside its method
  bodies, **not** at module import time. This satisfies the package's
  "no I/O at import" rule: importing ``forgejo_to_github.transport``
  must not perform any network or filesystem work.
* Each :meth:`RequestsTransport.__call__` creates a fresh
  ``requests.Session`` per call. This is intentional: the production
  client does not need connection pooling across calls (the CLI runs a
  short, single-threaded migration) and a per-call session avoids
  accidental state leaking between requests.
"""

from __future__ import annotations

from typing import Any, Protocol


class Transport(Protocol):
    """Callable HTTP transport.

    A transport is a single callable that takes a method, URL, and
    keyword-only arguments and returns a response-like object exposing
    ``status_code`` (int), ``headers`` (mapping of strings), ``url``
    (string), and a ``json()`` method returning the parsed body.

    Implementations must not raise ``requests``-specific exceptions
    unconditionally; clients translate non-protocol exceptions into
    their own structured error types. Tests commonly use exceptions
    like ``RuntimeError`` or bare ``Exception`` subclasses to simulate
    transport failures.
    """

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Issue an HTTP request and return a response-like object.

        Parameters
        ----------
        method:
            HTTP method (``"GET"``, ``"POST"``, ``"PATCH"``, ...).
        url:
            Fully qualified URL.
        params:
            Query-string parameters, forwarded verbatim to the underlying
            HTTP library.
        headers:
            Request headers, merged with whatever the underlying library
            adds by default.
        json_body:
            JSON-encodable body for ``POST`` / ``PATCH``. ``None`` means
            no body.
        timeout:
            Optional request timeout in seconds.
        """
        ...


class RequestsTransport:
    """Default :class:`Transport` adapter backed by :mod:`requests`.

    Each call constructs a fresh ``requests.Session`` and forwards the
    arguments to ``Session.request``. The ``requests`` package is
    imported lazily inside the method body so that importing this
    module performs no I/O.
    """

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Issue an HTTP request via :mod:`requests` and return the
        ``Response`` object.

        The ``requests`` module is imported lazily; an installation
        that lacks ``requests`` will only fail when this method is
        actually called.
        """
        import requests  # Lazy import: no module-level side effects.

        session = requests.Session()
        try:
            return session.request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                json=json_body,
                timeout=timeout,
            )
        finally:
            # Each call gets a fresh session, so close it eagerly to
            # release the underlying connection. The CLI does not
            # benefit from cross-call pooling.
            session.close()


__all__ = ["RequestsTransport", "Transport"]
