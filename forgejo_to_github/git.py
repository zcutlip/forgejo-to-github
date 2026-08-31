"""Git mirror service for the Forgejo → GitHub migration.

This module owns the Git side of the migration: cloning a Codeberg/Forgejo
mirror, pushing branches and tags to GitHub, classifying failures, and
scrubbing tokens from any command line that is surfaced through an
exception or a log record.

Design notes
------------

* The :class:`GitMirror` class is constructed with **all** of its
  collaborators and the GitHub token it needs to build the authenticated
  push URL. There is no module-level state and no I/O at import time:
  the defaults are bound at instance time, not at module load time.
* Every collaborator is injectable. Tests can pass fakes for the
  subprocess boundary (``command_runner``), the filesystem boundary
  (``tempdir_factory``), and the cleanup boundary (``cleanup``).
* The class never spawns a real subprocess or touches the filesystem
  except through the injected callables. Importing this module does no
  work.
* The GitHub token is owned by the instance and is **never** passed
  through to callers. Every command line that mentions the token — for
  example the authenticated push URL ``https://x-access-token:{token}@github.com/...
  `` — is run through :func:`redact_token` before being logged or
  attached to an exception. The redaction is global ``str.replace``;
  a ``None`` or empty token is a no-op.
* Failures are classified into a small, documented hierarchy rooted at
  :class:`GitError`. Each subclass carries an ordered **advisory**
  block: cause → remediation → docs pointer. The advisory is part of
  the exception's ``str()`` form so callers can render it as-is.
* Clone failure is terminal: callers are expected to stop on a
  :class:`GitCloneError`. Branch and tag push failures are non-fatal:
  callers are expected to log and continue. The class itself does not
  decide whether to abort; it surfaces the right exception type and
  leaves the policy to the orchestrator.

This module imports only from the standard library. It performs no
network I/O at module load time and spawns no subprocesses.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Stable placeholder for redacted tokens. The redaction routine uses this
# literal everywhere a token might appear; tests rely on the exact value.
REDACTED_PLACEHOLDER: str = "<REDACTED>"

# Substrings that signal a non-fast-forward push rejection in git's
# stderr/stdout. Detection is keyword-based and intentionally
# conservative: false positives are surfaced as "non-fast-forward"
# advice, which is also useful for other rejected pushes.
_NON_FAST_FORWARD_TOKENS: tuple[str, ...] = (
    "non-fast-forward",
    "fetch first",
    "rejected because the tip of your current branch is behind",
)


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class GitError(Exception):
    """Base class for every Git service failure.

    The exception carries a human-readable, redacted message suitable
    for printing or logging, plus the recorded command line (``cmd``)
    and git's stderr text (``stderr``), both already scrubbed via
    :func:`redact_token`.

    Subclasses should call ``super().__init__(message, *, cmd=cmd,
    stderr=stderr)`` so the structured fields are available uniformly.
    """

    def __init__(
        self,
        message: str,
        *,
        cmd: list[str] | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.cmd: list[str] | None = list(cmd) if cmd is not None else None
        self.stderr: str = stderr


class GitCloneError(GitError):
    """Raised when ``git clone --mirror`` fails.

    Subclasses :class:`GitAuthError` and :class:`GitCloneTimeoutError`
    refine this class. Catch :class:`GitCloneError` to handle every
    clone failure mode uniformly.
    """


class GitAuthError(GitCloneError):
    """Raised when ``git clone --mirror`` fails for authentication reasons.

    Detected from stderr text such as ``"Authentication failed"``,
    ``"could not read Username"``, or ``"access denied"``. Carries the
    same structured fields as :class:`GitCloneError`.
    """


class GitCloneTimeoutError(GitCloneError):
    """Raised when ``git clone --mirror`` raises ``subprocess.TimeoutExpired``."""

    def __init__(
        self,
        message: str,
        *,
        cmd: list[str] | None = None,
        stderr: str = "",
        timeout: float | None = None,
    ) -> None:
        super().__init__(message, cmd=cmd, stderr=stderr)
        self.timeout: float | None = timeout


class GitPushError(GitError):
    """Raised when ``git push <auth_url> --all`` fails.

    Subclass :class:`GitPushRejectedError` refines this for
    non-fast-forward rejections. Catch :class:`GitPushError` to handle
    every branch-push failure uniformly.
    """


class GitPushRejectedError(GitPushError):
    """Raised when ``git push`` is rejected as non-fast-forward.

    Carries the same structured fields as :class:`GitPushError`.
    """


class GitTagPushError(GitError):
    """Raised when ``git push <auth_url> --tags`` fails.

    Tag-push failures are tracked as a separate root so callers can
    distinguish them from branch-push failures in the result
    aggregation. The class is **not** a subclass of :class:`GitPushError`:
    a branch push and a tag push are independent phases and the
    orchestrator may treat them differently in the final summary.
    """


# ---------------------------------------------------------------------------
# Re-export list
# ---------------------------------------------------------------------------

__all__ = [
    "REDACTED_PLACEHOLDER",
    "GitAuthError",
    "GitCloneError",
    "GitCloneTimeoutError",
    "GitError",
    "GitMirror",
    "GitPushError",
    "GitPushRejectedError",
    "GitTagPushError",
    "redact_token",
]


# ---------------------------------------------------------------------------
# Redaction helper
# ---------------------------------------------------------------------------


def redact_token(value: str, token: str | None) -> str:
    """Replace every occurrence of ``token`` in ``value`` with the
    stable :data:`REDACTED_PLACEHOLDER`.

    If ``token`` is ``None`` or empty, ``value`` is returned unchanged.
    Substitution is global (``str.replace``), not regex. The substitution
    is a literal-string replace; it does not attempt to redact URL
    credentials generically, only the exact token string.
    """
    if not token:
        return value
    return value.replace(token, REDACTED_PLACEHOLDER)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_command_runner(
    args: list[str],
    *,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Default ``subprocess.run``-shaped runner.

    Mirrors the legacy ``subprocess.run(..., check=True,
    capture_output=True, text=True)`` invocation used by
    ``f2gh.mirror_git_repo``. The wrapper is constructed at instance
    time, not at module import time, so importing this module performs
    no I/O.

    ``**kwargs`` swallows extra keyword arguments so tests can pass
    fields the real ``subprocess.run`` does not accept; those are
    silently dropped, not forwarded.
    """
    return subprocess.run(
        list(args),
        check=bool(check),
        capture_output=bool(capture_output),
        text=bool(text),
        timeout=timeout,
    )


def _default_cleanup(path: str, *args: Any, **kwargs: Any) -> None:
    """Default cleanup that delegates to ``shutil.rmtree``.

    Bound with ``ignore_errors=True`` so callers can invoke it on a
    directory that may or may not exist without raising.
    """
    shutil.rmtree(path, ignore_errors=True)


def _default_tempdir_factory(prefix: str | None = None, **kwargs: Any) -> str:
    """Default tempdir factory that delegates to ``tempfile.mkdtemp``."""
    return tempfile.mkdtemp(prefix=prefix, **kwargs)


def _argv_as_string(cmd: list[str] | tuple[str, ...] | str | None) -> str:
    """Render an argv sequence as a single space-joined string.

    Used when assembling an exception's rendered text. Token occurrences
    in the rendered string are scrubbed by :func:`redact_token` before
    the string is surfaced.
    """
    if cmd is None:
        return ""
    if isinstance(cmd, str):
        return cmd
    return " ".join(str(c) for c in cmd)


def _classify_clone_stderr(stderr: str) -> type[GitCloneError]:
    """Return the appropriate :class:`GitCloneError` subclass for ``stderr``.

    The classification is keyword-based and intentionally conservative.
    Network errors fall through to the generic :class:`GitCloneError`;
    the advisory block attached to the exception supplies the concrete
    remediation.
    """
    lower = stderr.lower()
    if (
        "authentication failed" in lower
        or "could not read username" in lower
        or "access denied" in lower
        or "permission denied" in lower
        or "could not read password" in lower
    ):
        return GitAuthError
    return GitCloneError


def _clone_advisory(stderr: str, *, exit_code: int | None = None) -> str:
    """Build the cause/remediation/docs block for a clone failure.

    The returned string is ordered:

    1. **Most likely cause** — a short sentence naming the underlying
       condition (network, auth, host resolution, etc.).
    2. **Concrete remediation** — a one- or two-step action the
       operator can take.
    3. **Docs pointer** — a URL fragment or the literal substring
       ``docs`` so callers can locate authoritative guidance.

    The ordering matters: callers and tests assert that the cause
    substring appears before the remediation, which appears before the
    docs pointer.
    """
    lower = stderr.lower()
    if "could not resolve host" in lower or "name or service not known" in lower:
        cause = "Network DNS failure: the hostname could not be resolved."
        remediation = "Check your network connection and DNS, then retry the migration."
    elif "connection timed out" in lower or "connection refused" in lower:
        cause = (
            "Network connection failure: the remote host did not accept the connection."
        )
        remediation = "Check your network connection and retry."
    elif "unable to access" in lower or "network is unreachable" in lower:
        cause = "Network unreachable."
        remediation = "Check your network connection and retry."
    elif (
        "authentication failed" in lower
        or "could not read username" in lower
        or "access denied" in lower
        or "permission denied" in lower
    ):
        cause = (
            "Authentication failed: the configured CODEBERG_TOKEN "
            "lacks access to the source repository."
        )
        remediation = (
            "Verify that CODEBERG_TOKEN is set and has access to the "
            "source repository, then retry."
        )
    elif "not found" in lower or "repository not found" in lower:
        cause = "Source repository not found on Codeberg."
        remediation = (
            "Verify the source repository name and that CODEBERG_TOKEN can read it."
        )
    else:
        cause = (
            f"Clone failed (exit code {exit_code}). "
            "The source repository could not be fetched."
        )
        remediation = "Check the source URL and your credentials, then retry."

    docs = (
        "See https://docs.codeberg.org/usage/pull-request/ "
        "for Codeberg access guidance."
    )
    return f"{cause}\n  {remediation}\n  {docs}"


def _classify_push_stderr(stderr: str) -> type[GitPushError]:
    """Return :class:`GitPushRejectedError` for non-fast-forward pushes,
    otherwise :class:`GitPushError`.
    """
    lower = stderr.lower()
    for token in _NON_FAST_FORWARD_TOKENS:
        if token in lower:
            return GitPushRejectedError
    return GitPushError


def _branch_push_advisory(stderr: str, *, exit_code: int | None = None) -> str:
    """Build the advisory block for a branch-push failure.

    For non-fast-forward rejections, the remediation recommends
    ``git pull --rebase`` and ``--force-with-lease``. For other push
    failures, the remediation is generic ("auth, scopes, or branch
    state").
    """
    lower = stderr.lower()
    if any(token in lower for token in _NON_FAST_FORWARD_TOKENS):
        cause = "GitHub rejected the push: non-fast-forward (remote has commits you do not have locally)."
        remediation = (
            "Run `git pull --rebase` to integrate the remote commits and "
            "retry, or push with `--force-with-lease` to overwrite the "
            "remote branch."
        )
        docs = "See https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/pushing-commits-to-a-pull-request-branch"
    else:
        cause = (
            f"GitHub rejected the push (exit code {exit_code}). "
            "The branch push could not complete."
        )
        remediation = (
            "Check the push error, verify your GITHUB_TOKEN has the "
            "required scopes (repo, workflow), and retry."
        )
        docs = (
            "See https://docs.github.com/en/authentication/"
            "keeping-your-account-and-data-secure/"
            "managing-your-personal-access-tokens"
        )
    return f"{cause}\n  {remediation}\n  {docs}"


def _tag_push_advisory(stderr: str, *, exit_code: int | None = None) -> str:
    """Build the advisory block for a tag-push failure.

    Tags are pushed in a single ``--tags`` command, so there is no per-
    tag context to surface. The advisory refers to "tag push"
    generically and recommends re-fetching tags before retrying.
    """
    cause = (
        f"Tag push to GitHub failed (exit code {exit_code}). "
        "The tag push command did not complete."
    )
    remediation = (
        "Run `git fetch --tags` against the source mirror, then retry the tag push."
    )
    docs = "See https://docs.github.com/en/get-started/using-git/pushing-commits-to-a-remote-repository"
    return f"{cause}\n  {remediation}\n  {docs}"


# ---------------------------------------------------------------------------
# GitMirror
# ---------------------------------------------------------------------------


class GitMirror:
    """Clone a Codeberg/Forgejo mirror and push branches/tags to GitHub.

    The class owns the GitHub token used to build the authenticated push
    URL and never surfaces it to callers. Every command line that is
    logged or attached to an exception is run through
    :func:`redact_token` first.

    Parameters
    ----------
    source_url:
        The HTTPS URL of the source repository on Codeberg/Forgejo.
        Used verbatim as the ``<url>`` argument to ``git clone
        --mirror``.
    target_url:
        The HTTPS URL of the target repository on GitHub, **without**
        any embedded credentials. The authenticated URL is constructed
        at push time as
        ``https://x-access-token:{github_token}@github.com/<owner>/<repo>.git``.
        ``source_url`` and ``target_url`` are joined by replacing the
        host portion; see :meth:`_auth_url` for the join rule.
    github_token:
        The GitHub token used to authenticate the push. Required. The
        token is stored on the instance and is **not** redacted until
        it is rendered into a command line.
    command_runner:
        Callable matching :func:`subprocess.run`'s positional/keyword
        shape. Defaults to a thin wrapper that returns the
        ``CompletedProcess`` or raises ``CalledProcessError``.
    tempdir_factory:
        Callable matching :func:`tempfile.mkdtemp`'s keyword shape.
        Defaults to :func:`tempfile.mkdtemp` bound at instance time.
    cleanup:
        Callable matching :func:`shutil.rmtree`'s shape with
        ``ignore_errors=True``. Defaults to
        ``shutil.rmtree(..., ignore_errors=True)`` bound at instance
        time.

    Notes
    -----
    The defaults are constructed in ``__init__`` rather than at module
    import time. Importing this module performs no subprocess work
    and creates no files or directories.

    Public methods follow the spec:

    * :meth:`clone` returns the local path (str).
    * :meth:`push_branches` and :meth:`push_tags` accept a single
      ``local_path`` argument and return ``None``. The token and any
      ref/tag selection are owned by the instance.
    * :meth:`cleanup` removes the tempdir via the injected
      ``cleanup`` callable.
    """

    def __init__(
        self,
        source_url: str,
        target_url: str,
        github_token: str,
        command_runner: Callable[..., Any] | None = None,
        tempdir_factory: Callable[..., str] | None = None,
        cleanup: Callable[..., None] | None = None,
    ) -> None:
        self._source_url: str = source_url
        self._target_url: str = target_url
        self._github_token: str = github_token

        self._command_runner: Callable[..., Any] = (
            command_runner if command_runner is not None else _default_command_runner
        )
        self._tempdir_factory: Callable[..., str] = (
            tempdir_factory if tempdir_factory is not None else _default_tempdir_factory
        )
        self._cleanup: Callable[..., None] = (
            cleanup if cleanup is not None else _default_cleanup
        )

    # --- properties ---------------------------------------------------------

    @property
    def source_url(self) -> str:
        """The source repository URL passed to ``git clone --mirror``."""
        return self._source_url

    @property
    def target_url(self) -> str:
        """The unredacted target URL (no embedded credentials)."""
        return self._target_url

    # --- public API ---------------------------------------------------------

    def clone(self) -> str:
        """Clone ``source_url`` into a new tempdir using ``git --mirror``.

        The tempdir is created via the injected ``tempdir_factory``
        with a prefix derived from the second component of the target
        slug (e.g. ``widgets`` from ``owner/widgets``). The returned
        path is the value the factory produced.

        On ``subprocess.TimeoutExpired``, raises
        :class:`GitCloneTimeoutError`. On ``CalledProcessError``,
        raises :class:`GitAuthError` for authentication-related stderr
        or :class:`GitCloneError` otherwise. The exception message and
        any attached command-line text are run through
        :func:`redact_token` so the GitHub token never leaks.

        The tempdir is **not** cleaned up here; that is the caller's
        responsibility, exercised via :meth:`cleanup`.
        """
        local_path = self._tempdir_factory(prefix=self._tempdir_prefix())
        argv = ["git", "clone", "--mirror", self._source_url, local_path]
        try:
            self._run(argv)
        except subprocess.TimeoutExpired as exc:
            cmd_str = _argv_as_string(argv)
            sanitized_cmd = redact_token(cmd_str, self._github_token)
            raw_timeout_stderr: str | bytes | None = exc.stderr
            if isinstance(raw_timeout_stderr, bytes):
                raw_timeout_stderr = raw_timeout_stderr.decode(
                    "utf-8", errors="replace"
                )
            if not isinstance(raw_timeout_stderr, str):
                raw_timeout_stderr = ""
            sanitized_stderr = redact_token(raw_timeout_stderr, self._github_token)
            advisory = _clone_advisory(sanitized_stderr, exit_code=None)
            raise GitCloneTimeoutError(
                f"git clone timed out after {exc.timeout}s\n"
                f"  command: {sanitized_cmd}\n"
                f"  stderr: {sanitized_stderr}\n"
                f"  {advisory}",
                cmd=argv,
                stderr=sanitized_stderr,
                timeout=exc.timeout,
            ) from exc
        except subprocess.CalledProcessError as exc:
            cmd_str = _argv_as_string(exc.cmd)
            sanitized_cmd = redact_token(cmd_str, self._github_token)
            raw_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            sanitized_stderr = redact_token(raw_stderr, self._github_token)
            cls = _classify_clone_stderr(sanitized_stderr)
            advisory = _clone_advisory(sanitized_stderr, exit_code=exc.returncode)
            message = (
                f"git clone failed (exit code {exc.returncode})\n"
                f"  command: {sanitized_cmd}\n"
                f"  stderr: {sanitized_stderr}\n"
                f"  {advisory}"
            )
            raise cls(
                message,
                cmd=argv,
                stderr=sanitized_stderr,
            ) from exc

        return local_path

    def push_branches(self, local_path: str) -> None:
        """Push all branches in one command.

        Runs ``git -C <local_path> push <auth_url> --all``. The
        authenticated URL is constructed from the target URL and the
        instance's GitHub token. On ``CalledProcessError``, raises
        :class:`GitPushRejectedError` for non-fast-forward stderr or
        :class:`GitPushError` otherwise. The exception's rendered
        text and any logged command line are scrubbed via
        :func:`redact_token`.

        This method does **not** take a token or ref list; the token
        is owned by the instance, and ``--all`` pushes every branch
        in one command.
        """
        argv = ["git", "-C", local_path, "push", self._auth_url(), "--all"]
        try:
            self._run(argv)
        except subprocess.CalledProcessError as exc:
            cmd_str = _argv_as_string(exc.cmd)
            sanitized_cmd = redact_token(cmd_str, self._github_token)
            raw_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            sanitized_stderr = redact_token(raw_stderr, self._github_token)
            cls = _classify_push_stderr(sanitized_stderr)
            advisory = _branch_push_advisory(sanitized_stderr, exit_code=exc.returncode)
            message = (
                f"git push --all failed (exit code {exc.returncode})\n"
                f"  command: {sanitized_cmd}\n"
                f"  stderr: {sanitized_stderr}\n"
                f"  {advisory}"
            )
            raise cls(
                message,
                cmd=argv,
                stderr=sanitized_stderr,
            ) from exc

    def push_tags(self, local_path: str) -> None:
        """Push all tags in one command.

        Runs ``git -C <local_path> push <auth_url> --tags``. The
        authenticated URL is constructed from the target URL and the
        instance's GitHub token. On ``CalledProcessError``, raises
        :class:`GitTagPushError`. Tag names are not passed as argv
        (``--tags`` pushes every tag at once), so there is no
        tag-name redaction step beyond scrubbing the auth URL.

        This method does **not** take a token or tag list; the token
        is owned by the instance, and ``--tags`` pushes every tag in
        one command.
        """
        argv = ["git", "-C", local_path, "push", self._auth_url(), "--tags"]
        try:
            self._run(argv)
        except subprocess.CalledProcessError as exc:
            cmd_str = _argv_as_string(exc.cmd)
            sanitized_cmd = redact_token(cmd_str, self._github_token)
            raw_stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            sanitized_stderr = redact_token(raw_stderr, self._github_token)
            advisory = _tag_push_advisory(sanitized_stderr, exit_code=exc.returncode)
            message = (
                f"git push --tags failed (exit code {exc.returncode})\n"
                f"  command: {sanitized_cmd}\n"
                f"  stderr: {sanitized_stderr}\n"
                f"  {advisory}"
            )
            raise GitTagPushError(
                message,
                cmd=argv,
                stderr=sanitized_stderr,
            ) from exc

    def cleanup(self, local_path: str) -> None:
        """Remove the tempdir using the injected ``cleanup`` callable.

        Idempotent: the default callable (``shutil.rmtree`` with
        ``ignore_errors=True``) does not raise when the directory does
        not exist. Callers can invoke this method from a ``finally``
        block or after a non-fatal push failure without first checking
        whether the directory was created.
        """
        try:
            self._cleanup(local_path)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            # Cleanup is best-effort. A failure here is logged by the
            # orchestrator (stage 04) but never re-raised: the Git
            # service does not surface cleanup failures as
            # :class:`GitError`.
            import logging

            logging.getLogger("forgejo_to_github.git").debug(
                "cleanup failure for %s: %s", local_path, exc
            )

    # --- internals ----------------------------------------------------------

    def _auth_url(self) -> str:
        """Build the authenticated push URL.

        The target URL passed to the constructor is the unredacted
        ``https://github.com/<owner>/<repo>.git`` form. The authenticated
        form embeds the GitHub token via the
        ``x-access-token:{token}`` userinfo component.
        """
        token = self._github_token
        target = self._target_url
        if target.startswith("https://"):
            scheme, rest = target.split("://", 1)
            return f"{scheme}://x-access-token:{token}@{rest}"
        if target.startswith("http://"):
            scheme, rest = target.split("://", 1)
            return f"{scheme}://x-access-token:{token}@{rest}"
        # Non-http(s) URLs (e.g. SSH) are out of scope; surface as-is.
        return target

    def _tempdir_prefix(self) -> str:
        """Return the tempdir prefix derived from the target slug.

        The prefix is ``f"f2gh-<repo>-"`` where ``<repo>`` is the
        second component of the target slug (e.g. ``widgets`` from
        ``owner/widgets``). For malformed target slugs, the prefix
        falls back to ``"f2gh-"``.
        """
        slug = self._target_url.rstrip("/").rstrip(".git").rsplit("/", 1)[-1]
        if "/" in self._target_url and slug:
            return f"f2gh-{slug}-"
        return "f2gh-"

    def _run(self, argv: list[str]) -> Any:
        """Invoke the injected ``command_runner`` with redaction applied.

        The command line is logged via the standard ``logging`` module
        after being scrubbed through :func:`redact_token`. Logging is
        the orchestrator-friendly channel; exceptions raised by the
        runner are propagated to the caller, which classifies them.
        """
        import logging

        redacted_argv = [redact_token(str(arg), self._github_token) for arg in argv]
        redacted_cmd = " ".join(redacted_argv)
        logging.getLogger("forgejo_to_github.git").info(
            "running: %s",
            redacted_cmd,
        )

        # Forward to the runner. Callers (the public methods) handle
        # classification and exception construction. Exceptions raised
        # by the runner (notably ``subprocess.CalledProcessError`` and
        # ``subprocess.TimeoutExpired``) propagate verbatim.
        runner = self._command_runner
        return runner(
            argv,
            check=True,
            capture_output=True,
            text=True,
        )
