"""Local-checkout source inference contract (issue #6).

Covers deriving the migration ``owner/repo`` slug from the current
working directory instead of requiring an explicit ``--source``:

* Codeberg origin URL parsing (HTTPS, SSH, and ``git@`` scp forms with
  an optional ``.git`` suffix; host match case-insensitive, owner/repo
  case preserved; GitHub and ``Host``-alias origins rejected).
* Origin resolution through ``git ls-remote --get-url origin`` so
  ``url.insteadOf`` rewrites apply (empty output or a nonzero exit
  means "no origin").
* Cwd validation order — work tree, then origin, then Codeberg match,
  then not-shallow, then not-partial — with one usage message per
  first failure, and a resolved absolute path on success.
* Source resolution: explicit slugs pass through, an omitted slug is
  inferred behind a single confirm prompt (skipped by ``--cwd``),
  ``--yes`` requires an explicit source, and a ``--cwd``/explicit
  mismatch is a usage error.
* Target resolution: explicit slugs pass through, an omitted target
  defaults to ``<gh-user>/<source-repo>`` from the credential actually
  used, and ``--yes`` requires an explicit target.
* ``GET /user`` login lookup against the resolved token, including the
  transport-failure mapping.
* Clone-source URL selection (absolutized local path verbatim, never a
  ``file://`` URL; Codeberg HTTPS otherwise).
* ``--clean`` target handling: an explicit target is required (plain,
  ``--cwd``, and ``--dry-run`` forms alike) and resolving it stays
  offline with no token read.

Conventions mirror ``tests/test_clone_cache.py``: a file-local
scripted runner keyed on ``tuple(argv)`` returning
``SimpleNamespace(returncode, stdout, stderr)`` with every call
(including kwargs) recorded, ``argparse.Namespace`` values built
directly, and ``SystemExit`` codes asserted via ``excinfo.value.code``.
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import FrozenInstanceError, dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from forgejo_to_github.cwd_source import (
    CwdError,
    CwdSource,
    infer_cwd_source,
    parse_codeberg_slug,
    resolve_origin_url,
)

from f2gh import git_source_url, resolve_clean_target, resolve_source, resolve_target
from forgejo_to_github.github import GitHubClient, GitHubError

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _ok(stdout: str = "") -> SimpleNamespace:
    """Build a successful runner result carrying ``stdout``."""
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _failed(
    returncode: int = 1, stdout: str = "", stderr: str = ""
) -> SimpleNamespace:
    """Build a failed runner result shaped like git's failure output."""
    return SimpleNamespace(
        returncode=returncode, stdout=stdout, stderr=stderr
    )


@dataclass
class _ScriptedRunner:
    """Git command runner routing an exact argv tuple to a result.

    Routes map ``tuple(argv)`` to a ``SimpleNamespace(returncode,
    stdout, stderr)`` result (or a ``BaseException`` to raise).
    Every call — argv plus kwargs — is recorded in ``calls``.
    """

    routes: dict[tuple[str, ...], SimpleNamespace | BaseException] = field(
        default_factory=dict
    )
    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> SimpleNamespace:
        self.calls.append((list(argv), dict(kwargs)))
        value = self.routes.get(tuple(argv))
        if isinstance(value, BaseException):
            raise value
        if value is not None:
            return value
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    def argvs(self) -> list[list[str]]:
        """Return the recorded argv lists in call order."""
        return [argv for argv, _ in self.calls]


def _cwd_runner(origin_url: str) -> _ScriptedRunner:
    """Build a runner scripted for a healthy ``owner/repo`` checkout."""
    return _ScriptedRunner(
        routes={
            ("git", "rev-parse", "--is-inside-work-tree"): _ok("true\n"),
            ("git", "ls-remote", "--get-url", "origin"): _ok(origin_url + "\n"),
            ("git", "rev-parse", "--is-shallow-repository"): _ok("false\n"),
            ("git", "config", "--get", "extensions.partialclone"): _failed(1),
        }
    )


@dataclass
class _ScriptedPrompter:
    """Confirm prompter returning a fixed answer, recording prompts."""

    response: bool
    prompts: list[str] = field(default_factory=list)

    def __call__(self, prompt: str, *args: Any, **kwargs: Any) -> bool:
        self.prompts.append(prompt)
        return self.response


def _refusing_prompter(prompt: str, *args: Any, **kwargs: Any) -> bool:
    """Prompter that fails the test if inference prompts at all."""
    raise AssertionError(f"prompter must not be called, got: {prompt!r}")


@dataclass
class _ScriptedResolver:
    """``GET /user`` stand-in returning a fixed login (or raising)."""

    value: str | None = None
    error: BaseException | None = None
    calls: list[None] = field(default_factory=list)

    def __call__(self, *args: Any, **kwargs: Any) -> str | None:
        self.calls.append(None)
        if self.error is not None:
            raise self.error
        return self.value


@dataclass
class _FakeResponse:
    """Minimal stand-in for an HTTP response."""

    status_code: int
    json_payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        if self.json_payload is None:
            raise ValueError("no json body")
        return self.json_payload


class _FakeTransport:
    """In-memory HTTP transport with a scripted response queue."""

    def __init__(self, script: list[_FakeResponse | BaseException]) -> None:
        self._script: list[_FakeResponse | BaseException] = list(script)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "json_body": json_body,
                "timeout": timeout,
            }
        )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _make_args(
    *,
    source: str | None = None,
    target: str | None = None,
    cwd: bool = False,
    yes: bool = False,
    dry_run: bool = False,
) -> argparse.Namespace:
    """Build the CLI namespace subset the source/target resolvers read."""
    return argparse.Namespace(
        source=source, target=target, cwd=cwd, yes=yes, dry_run=dry_run
    )


def _user_client(transport: _FakeTransport, token: str) -> GitHubClient:
    """Build a GitHubClient wired to the fake transport for /user tests."""
    return GitHubClient(
        base_url="https://api.github.com",
        owner="owner",
        repo="repo",
        token=token,
        transport=transport,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# A. parse_codeberg_slug matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://codeberg.org/o/r", ("o", "r")),
        ("https://codeberg.org/o/r.git", ("o", "r")),
        ("https://codeberg.org/o/r/", ("o", "r")),
        ("ssh://git@codeberg.org/o/r.git", ("o", "r")),
        ("git@codeberg.org:o/r.git", ("o", "r")),
        ("https://CODEBERG.ORG/Owner/Repo", ("Owner", "Repo")),
    ],
)
def test_parse_codeberg_slug_accepts_codeberg_forms(
    url: str, expected: tuple[str, str]
) -> None:
    """Codeberg HTTPS/SSH/scp origins yield ``(owner, repo)``."""
    assert parse_codeberg_slug(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/o/r",
        "git@github.com:o/r.git",
        "cb:o/r",
        "https://codeberg.org/owner",
        "https://codeberg.org/",
        "",
    ],
)
def test_parse_codeberg_slug_rejects_non_codeberg_forms(url: str) -> None:
    """GitHub origins, Host aliases, and owner/repo-less URLs yield None."""
    assert parse_codeberg_slug(url) is None


# ---------------------------------------------------------------------------
# B. resolve_origin_url
# ---------------------------------------------------------------------------


def test_resolve_origin_url_uses_ls_remote_get_url() -> None:
    """The origin comes from ``git ls-remote --get-url origin``, stripped."""
    runner = _ScriptedRunner(
        routes={
            ("git", "ls-remote", "--get-url", "origin"): _ok(
                "https://codeberg.org/o/r.git\n"
            ),
        }
    )

    assert resolve_origin_url(runner) == "https://codeberg.org/o/r.git"
    assert runner.argvs() == [["git", "ls-remote", "--get-url", "origin"]]


def test_resolve_origin_url_empty_stdout_is_none() -> None:
    """Empty stdout means the checkout has no origin."""
    runner = _ScriptedRunner(
        routes={("git", "ls-remote", "--get-url", "origin"): _ok("\n")}
    )

    assert resolve_origin_url(runner) is None


def test_resolve_origin_url_nonzero_return_is_none() -> None:
    """A nonzero exit means the checkout has no origin."""
    runner = _ScriptedRunner(
        routes={("git", "ls-remote", "--get-url", "origin"): _failed(128)}
    )

    assert resolve_origin_url(runner) is None


# ---------------------------------------------------------------------------
# C. infer_cwd_source order, messages, and success shape
# ---------------------------------------------------------------------------


def test_cwd_source_is_frozen() -> None:
    """CwdSource is a frozen value object, never mutated in place."""
    source = CwdSource(
        slug="o/r",
        path="/tmp/work/checkout",
        origin_url="https://codeberg.org/o/r.git",
    )

    with pytest.raises(FrozenInstanceError):
        source.slug = "other/repo"  # type: ignore[misc]


def test_infer_cwd_source_outside_work_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a work tree the run stops before reading any origin."""
    monkeypatch.chdir(tmp_path)
    runner = _ScriptedRunner(
        routes={
            ("git", "rev-parse", "--is-inside-work-tree"): _failed(
                128, "false\n"
            ),
        }
    )

    with pytest.raises(CwdError) as exc_info:
        infer_cwd_source(runner)

    assert str(exc_info.value) == "cwd is not inside a git work tree"
    assert runner.argvs() == [["git", "rev-parse", "--is-inside-work-tree"]]


def test_infer_cwd_source_without_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A work tree with no origin stops before any Codeberg check."""
    monkeypatch.chdir(tmp_path)
    runner = _ScriptedRunner(
        routes={
            ("git", "rev-parse", "--is-inside-work-tree"): _ok("true\n"),
            ("git", "ls-remote", "--get-url", "origin"): _failed(1),
        }
    )

    with pytest.raises(CwdError) as exc_info:
        infer_cwd_source(runner)

    assert str(exc_info.value) == "cwd has no git origin"
    assert ("git", "rev-parse", "--is-shallow-repository") not in [
        tuple(argv) for argv in runner.argvs()
    ]


def test_infer_cwd_source_non_codeberg_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-Codeberg origin is reported with the offending URL."""
    monkeypatch.chdir(tmp_path)
    runner = _ScriptedRunner(
        routes={
            ("git", "rev-parse", "--is-inside-work-tree"): _ok("true\n"),
            ("git", "ls-remote", "--get-url", "origin"): _ok(
                "https://github.com/o/r.git\n"
            ),
        }
    )

    with pytest.raises(CwdError) as exc_info:
        infer_cwd_source(runner)

    assert (
        str(exc_info.value)
        == "origin 'https://github.com/o/r.git' is not a Codeberg remote"
    )
    assert ("git", "rev-parse", "--is-shallow-repository") not in [
        tuple(argv) for argv in runner.argvs()
    ]


def test_infer_cwd_source_shallow_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shallow checkout stops before the partial-clone probe."""
    monkeypatch.chdir(tmp_path)
    runner = _ScriptedRunner(
        routes={
            ("git", "rev-parse", "--is-inside-work-tree"): _ok("true\n"),
            ("git", "ls-remote", "--get-url", "origin"): _ok(
                "https://codeberg.org/o/r.git\n"
            ),
            ("git", "rev-parse", "--is-shallow-repository"): _ok("true\n"),
        }
    )

    with pytest.raises(CwdError) as exc_info:
        infer_cwd_source(runner)

    assert (
        str(exc_info.value)
        == "cwd is a shallow checkout; re-clone without --depth"
    )
    assert ("git", "config", "--get", "extensions.partialclone") not in [
        tuple(argv) for argv in runner.argvs()
    ]


def test_infer_cwd_source_partial_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial clone is rejected even when every earlier check passes."""
    monkeypatch.chdir(tmp_path)
    runner = _ScriptedRunner(
        routes={
            ("git", "rev-parse", "--is-inside-work-tree"): _ok("true\n"),
            ("git", "ls-remote", "--get-url", "origin"): _ok(
                "https://codeberg.org/o/r.git\n"
            ),
            ("git", "rev-parse", "--is-shallow-repository"): _ok("false\n"),
            ("git", "config", "--get", "extensions.partialclone"): _ok(
                "promisor\n"
            ),
        }
    )

    with pytest.raises(CwdError) as exc_info:
        infer_cwd_source(runner)

    assert (
        str(exc_info.value)
        == "cwd is a partial clone; re-clone without --filter"
    )


def test_infer_cwd_source_success_returns_resolved_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A healthy checkout yields the slug plus the absolutized cwd."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")

    result = infer_cwd_source(runner)

    expected_path = str(Path.cwd().resolve())
    assert result == CwdSource(
        slug="o/r",
        path=expected_path,
        origin_url="https://codeberg.org/o/r.git",
    )
    # The probes run in contract order: work tree, origin, Codeberg
    # match (no subprocess), shallow, partial.
    assert runner.argvs() == [
        ["git", "rev-parse", "--is-inside-work-tree"],
        ["git", "ls-remote", "--get-url", "origin"],
        ["git", "rev-parse", "--is-shallow-repository"],
        ["git", "config", "--get", "extensions.partialclone"],
    ]


# ---------------------------------------------------------------------------
# D. resolve_source
# ---------------------------------------------------------------------------


def test_resolve_source_explicit_passes_through_without_prompt() -> None:
    """An explicit source needs no inference and no prompt."""
    slug, cwd_source = resolve_source(
        _make_args(source="o/r"), _ScriptedRunner(), _refusing_prompter
    )

    assert slug == "o/r"
    assert cwd_source is None


@pytest.mark.parametrize("source", ["not-a-slug", "o/", "/r"])
def test_resolve_source_explicit_malformed_exits_usage(source: str) -> None:
    """An explicit source that is not OWNER/REPO is a usage error."""
    with pytest.raises(SystemExit) as exc_info:
        resolve_source(
            _make_args(source=source), _ScriptedRunner(), _refusing_prompter
        )

    assert exc_info.value.code == 2


def test_resolve_source_omitted_prompts_with_slug_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accepting the inference prompt yields the slug and its source."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")
    prompter = _ScriptedPrompter(response=True)

    slug, cwd_source = resolve_source(_make_args(), runner, prompter)

    assert slug == "o/r"
    assert cwd_source is not None
    assert cwd_source.slug == "o/r"
    assert cwd_source.path == str(Path.cwd().resolve())
    assert len(prompter.prompts) == 1
    assert "o/r" in prompter.prompts[0]
    assert cwd_source.path in prompter.prompts[0]


def test_resolve_source_deny_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Declining the inference prompt aborts before anything mutates."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")

    with pytest.raises(SystemExit) as exc_info:
        resolve_source(_make_args(), runner, _ScriptedPrompter(response=False))

    assert exc_info.value.code == 1


def test_resolve_source_non_tty_deny_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-tty EOF deny (prompter False) aborts like an explicit No."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")

    with pytest.raises(SystemExit) as exc_info:
        resolve_source(_make_args(), runner, _ScriptedPrompter(response=False))

    assert exc_info.value.code == 1


def test_resolve_source_cwd_flag_skips_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--cwd expresses intent up front, so no inference prompt fires."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")
    prompter = _ScriptedPrompter(response=True)

    slug, cwd_source = resolve_source(_make_args(cwd=True), runner, prompter)

    assert slug == "o/r"
    assert cwd_source is not None
    assert cwd_source.slug == "o/r"
    assert prompter.prompts == []


def test_resolve_source_cwd_with_matching_explicit_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--cwd plus a matching --source resolves locally with no prompt."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")
    prompter = _ScriptedPrompter(response=True)

    slug, cwd_source = resolve_source(
        _make_args(source="o/r", cwd=True), runner, prompter
    )

    assert slug == "o/r"
    assert cwd_source is not None
    assert prompter.prompts == []


def test_resolve_source_cwd_with_mismatching_source_exits_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--cwd plus a conflicting --source never silently prefers one."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")

    with pytest.raises(SystemExit) as exc_info:
        resolve_source(
            _make_args(source="other/r", cwd=True),
            runner,
            _ScriptedPrompter(response=True),
        )

    assert exc_info.value.code == 2


def test_resolve_source_yes_without_source_or_cwd_exits_usage() -> None:
    """--yes cannot confirm an inference that never happens."""
    runner = _ScriptedRunner()

    with pytest.raises(SystemExit) as exc_info:
        resolve_source(
            _make_args(yes=True), runner, _ScriptedPrompter(response=True)
        )

    assert exc_info.value.code == 2
    assert runner.calls == []


def test_resolve_source_yes_with_cwd_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--yes combined with --cwd resolves without prompting."""
    monkeypatch.chdir(tmp_path)
    runner = _cwd_runner("https://codeberg.org/o/r.git")
    prompter = _ScriptedPrompter(response=True)

    slug, cwd_source = resolve_source(
        _make_args(yes=True, cwd=True), runner, prompter
    )

    assert slug == "o/r"
    assert cwd_source is not None
    assert prompter.prompts == []


def test_resolve_source_yes_with_explicit_source() -> None:
    """--yes combined with an explicit source passes straight through."""
    prompter = _ScriptedPrompter(response=True)

    slug, cwd_source = resolve_source(
        _make_args(source="o/r", yes=True), _ScriptedRunner(), prompter
    )

    assert slug == "o/r"
    assert cwd_source is None
    assert prompter.prompts == []


# ---------------------------------------------------------------------------
# E. resolve_target
# ---------------------------------------------------------------------------


def test_resolve_target_explicit_passes_through() -> None:
    """An explicit target needs no login lookup."""
    resolver = _ScriptedResolver(value="someone")

    assert (
        resolve_target(_make_args(target="gh-user/r"), "o/r", resolver)
        == "gh-user/r"
    )
    assert resolver.calls == []


def test_resolve_target_explicit_malformed_exits_usage() -> None:
    """An explicit target that is not OWNER/REPO is a usage error."""
    with pytest.raises(SystemExit) as exc_info:
        resolve_target(
            _make_args(target="not-a-slug"), "o/r", _ScriptedResolver()
        )

    assert exc_info.value.code == 2


def test_resolve_target_omitted_derives_login_repo() -> None:
    """An omitted target defaults to <gh-user>/<source-repo>."""
    resolver = _ScriptedResolver(value="token-user")

    assert resolve_target(_make_args(), "o/r", resolver) == "token-user/r"


def test_resolve_target_lookup_none_exits_usage() -> None:
    """A login lookup yielding None requires an explicit --target."""
    with pytest.raises(SystemExit) as exc_info:
        resolve_target(_make_args(), "o/r", _ScriptedResolver(value=None))

    assert exc_info.value.code == 2


def test_resolve_target_lookup_failure_exits_usage() -> None:
    """A failing login lookup requires an explicit --target."""
    resolver = _ScriptedResolver(error=GitHubError("boom"))

    with pytest.raises(SystemExit) as exc_info:
        resolve_target(_make_args(), "o/r", resolver)

    assert exc_info.value.code == 2


def test_resolve_target_yes_without_target_exits_usage() -> None:
    """--yes cannot confirm a default target that needs a lookup."""
    resolver = _ScriptedResolver(value="token-user")

    with pytest.raises(SystemExit) as exc_info:
        resolve_target(_make_args(yes=True), "o/r", resolver)

    assert exc_info.value.code == 2
    assert resolver.calls == []


# ---------------------------------------------------------------------------
# F. get_current_user
# ---------------------------------------------------------------------------


def test_get_current_user_returns_login_with_resolved_token() -> None:
    """GET /user yields its login, authorized by the resolved token."""
    transport = _FakeTransport([_FakeResponse(200, {"login": "token-user"})])
    client = _user_client(transport, "resolved-token")

    assert client.get_current_user() == "token-user"

    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://api.github.com/user"
    headers = call["headers"] or {}
    assert headers.get("Authorization") == "Bearer resolved-token"


def test_get_current_user_transport_failure_raises_base_error() -> None:
    """A transport failure surfaces as the module's base error."""
    transport = _FakeTransport([ConnectionError("network down")])
    client = _user_client(transport, "resolved-token")

    with pytest.raises(GitHubError):
        client.get_current_user()


# ---------------------------------------------------------------------------
# G. git_source_url
# ---------------------------------------------------------------------------


def test_git_source_url_local_returns_path_verbatim() -> None:
    """A validated checkout clones from its path, never a file:// URL."""
    source = CwdSource(
        slug="o/r",
        path="/tmp/work/checkout",
        origin_url="https://codeberg.org/o/r.git",
    )

    result = git_source_url("o/r", source)

    assert result == "/tmp/work/checkout"
    assert not result.startswith("file://")


def test_git_source_url_without_local_uses_codeberg_https() -> None:
    """Without a local checkout the clone goes over the network."""
    assert git_source_url("o/r", None) == "https://codeberg.org/o/r.git"


# ---------------------------------------------------------------------------
# H. resolve_clean_target
# ---------------------------------------------------------------------------


def test_resolve_clean_target_without_target_exits_usage() -> None:
    """--clean without --target stops before any subprocess runs."""
    runner = _ScriptedRunner()

    with pytest.raises(SystemExit) as exc_info:
        resolve_clean_target(_make_args(source="o/r"))

    assert exc_info.value.code == 2
    assert runner.calls == []


def test_resolve_clean_target_cwd_without_target_exits_usage() -> None:
    """--clean --cwd still requires an explicit --target."""
    runner = _ScriptedRunner()

    with pytest.raises(SystemExit) as exc_info:
        resolve_clean_target(_make_args(cwd=True))

    assert exc_info.value.code == 2
    assert runner.calls == []


def test_resolve_clean_target_dry_run_without_target_exits_usage() -> None:
    """--clean --dry-run still requires an explicit --target."""
    runner = _ScriptedRunner()

    with pytest.raises(SystemExit) as exc_info:
        resolve_clean_target(_make_args(source="o/r", dry_run=True))

    assert exc_info.value.code == 2
    assert runner.calls == []


def test_resolve_clean_target_cwd_with_target_stays_tokenless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--clean --cwd --target resolves with no token read and no network."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("CODEBERG_TOKEN", raising=False)

    def _forbidden_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("resolve_clean_target must stay offline")

    monkeypatch.setattr(subprocess, "run", _forbidden_run)

    assert (
        resolve_clean_target(_make_args(cwd=True, target="gh-user/r"))
        == "gh-user/r"
    )
