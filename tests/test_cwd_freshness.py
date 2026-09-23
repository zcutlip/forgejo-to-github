"""Local-checkout freshness contract (issue #6).

Covers read-only staleness probing before a local clone: the remote
``ls-remote`` tip map, the local ``show-ref`` map, per-branch ancestor
checks via ``merge-base --is-ancestor``, the unified freshness result,
its prompt/notice formatting, the dirt notice, and the gate that prints
the ``Using local checkout`` line, warns on probe failure, and prompts
once when behind/divergent/missing/moved refs exist.

Conventions mirror ``tests/test_clone_cache.py``: a file-local
scripted runner keyed on ``tuple(argv)`` returning
``SimpleNamespace(returncode, stdout, stderr)`` with every call
(including kwargs) recorded, and ``SystemExit`` codes asserted via
``excinfo.value.code``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from forgejo_to_github.cwd_source import (
    CwdSource,
    FreshnessResult,
    ProbeError,
    check_freshness,
    format_freshness_prompt,
    format_local_only_notice,
    has_uncommitted_changes,
    local_ref_map,
    ref_contains,
    remote_ref_tips,
)

from f2gh import gate_local_source

ORIGIN_URL = "https://codeberg.org/o/r.git"
CWD_PATH = "/tmp/work/checkout"

MAIN_REMOTE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MAIN_LOCAL = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
FEATURE_REMOTE = "cccccccccccccccccccccccccccccccccccccccc"
TAG_V13 = "dddddddddddddddddddddddddddddddddddddddd"
TAG_V20_REMOTE = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
TAG_V20_LOCAL = "ffffffffffffffffffffffffffffffffffffffff"


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

    def __call__(self, argv: list[str], **kwargs: Any) -> SimpleNamespace:
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

    def kwargs_for(self, argv: list[str]) -> dict[str, Any]:
        """Return the kwargs recorded for the first matching argv."""
        for recorded_argv, recorded_kwargs in self.calls:
            if recorded_argv == argv:
                return recorded_kwargs
        raise AssertionError(f"no recorded call for {argv!r}")

    def merge_base_calls(self) -> list[list[str]]:
        """Return recorded ``merge-base --is-ancestor`` argv lists."""
        return [
            argv
            for argv, _ in self.calls
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]
        ]


@dataclass
class _ScriptedPrompter:
    """Confirm prompter returning a fixed answer, recording prompts."""

    response: bool
    prompts: list[str] = field(default_factory=list)

    def __call__(self, prompt: str, *args: Any, **kwargs: Any) -> bool:
        self.prompts.append(prompt)
        return self.response


def _make_cwd(
    path: str = CWD_PATH,
    origin_url: str = ORIGIN_URL,
    slug: str = "o/r",
) -> CwdSource:
    """Build a ``CwdSource`` value for gate tests."""
    return CwdSource(slug=slug, path=path, origin_url=origin_url)


def _ls_remote_stdout(lines: list[str]) -> str:
    """Join raw ``ls-remote`` lines into probe stdout."""
    return "".join(line + "\n" for line in lines)


def _show_ref_stdout(lines: list[str]) -> str:
    """Join raw ``show-ref`` lines into local-map stdout."""
    return "".join(line + "\n" for line in lines)


def _merge_base_routes(
    remote_sha: str, local_sha: str, returncode: int
) -> dict[tuple[str, ...], SimpleNamespace]:
    """Cover the plausible ``merge-base`` second-arg forms.

    The contract pins ``["git", "merge-base", "--is-ancestor", sha,
    ref]`` but leaves whether ``ref`` is the local tip sha or the
    branch refname to the implementation; scripting every plausible
    form keeps the tests on the containment behavior instead of the
    spelling.
    """
    if returncode == 0:
        result = _ok()
    else:
        result = _failed(returncode)
    return {
        ("git", "merge-base", "--is-ancestor", remote_sha, local_sha): result,
        (
            "git",
            "merge-base",
            "--is-ancestor",
            remote_sha,
            "refs/heads/main",
        ): result,
        ("git", "merge-base", "--is-ancestor", remote_sha, "main"): result,
    }


def _freshness_runner(
    remote_lines: list[str],
    local_lines: list[str],
    *,
    merge_base_rc: dict[str, int] | None = None,
    ls_remote_rc: int = 0,
    dirty: bool = False,
) -> _ScriptedRunner:
    """Build a runner scripted for one ``check_freshness`` scenario."""
    if ls_remote_rc == 0:
        ls_result: SimpleNamespace = _ok(_ls_remote_stdout(remote_lines))
    else:
        ls_result = _failed(ls_remote_rc, stderr="fatal: unable to connect")
    routes: dict[tuple[str, ...], SimpleNamespace | BaseException] = {
        ("git", "ls-remote", ORIGIN_URL): ls_result,
        ("git", "show-ref"): _ok(_show_ref_stdout(local_lines)),
        ("git", "status", "--porcelain"): _ok(" M dirty.py\n" if dirty else ""),
    }
    if merge_base_rc:
        for remote_sha, returncode in merge_base_rc.items():
            # The local tip for ``main`` in these scenarios is MAIN_LOCAL;
            # cover sha and refname spellings for each remote sha.
            routes.update(_merge_base_routes(remote_sha, MAIN_LOCAL, returncode))
    return _ScriptedRunner(routes=routes)


def _clean_lines(sha: str = MAIN_REMOTE) -> tuple[list[str], list[str]]:
    """Return matching remote/local line lists for a clean checkout."""
    remote = [
        f"{sha}\trefs/heads/main",
        f"{TAG_V13}\trefs/tags/v1.3",
    ]
    local = [
        f"{sha} refs/heads/main",
        f"{TAG_V13} refs/tags/v1.3",
    ]
    return remote, local


# ---------------------------------------------------------------------------
# A. remote_ref_tips
# ---------------------------------------------------------------------------


def test_remote_ref_tips_parses_ls_remote_stdout() -> None:
    """Ls-remote ``sha TAB ref`` lines become a ``{ref: sha}`` map."""
    runner = _ScriptedRunner(
        routes={
            ("git", "ls-remote", ORIGIN_URL): _ok(
                f"{MAIN_REMOTE}\trefs/heads/main\n"
                f"{TAG_V13}\trefs/tags/v1.3\n"
            ),
        }
    )

    result = remote_ref_tips(runner, ORIGIN_URL)

    assert result == {
        "refs/heads/main": MAIN_REMOTE,
        "refs/tags/v1.3": TAG_V13,
    }
    assert runner.argvs() == [["git", "ls-remote", ORIGIN_URL]]


def test_remote_ref_tips_sends_no_prompt_ssh_env() -> None:
    """The remote probe fails fast instead of blocking on credentials."""
    runner = _ScriptedRunner(
        routes={("git", "ls-remote", ORIGIN_URL): _ok("")}
    )

    remote_ref_tips(runner, ORIGIN_URL)

    kwargs = runner.kwargs_for(["git", "ls-remote", ORIGIN_URL])
    env = kwargs.get("env", {})
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    assert env.get("GIT_SSH_COMMAND") == "ssh -o BatchMode=yes"


def test_remote_ref_tips_drops_synthetic_peeled_and_malformed() -> None:
    """Pull/notes namespaces, peeled tag lines, and junk never classify."""
    runner = _ScriptedRunner(
        routes={
            ("git", "ls-remote", ORIGIN_URL): _ok(
                f"{MAIN_REMOTE}\trefs/heads/main\n"
                "1111111111111111111111111111111111111111\trefs/pull/1/head\n"
                "2222222222222222222222222222222222222222\trefs/notes/commits\n"
                f"{TAG_V13}\trefs/tags/v1.3\n"
                f"{TAG_V13}\trefs/tags/v1.3^{{}}\n"
                "not-a-valid-line-without-tab\n"
                "\n"
            ),
        }
    )

    result = remote_ref_tips(runner, ORIGIN_URL)

    assert result == {
        "refs/heads/main": MAIN_REMOTE,
        "refs/tags/v1.3": TAG_V13,
    }


def test_remote_ref_tips_nonzero_return_raises_probe_error() -> None:
    """A failed ls-remote surfaces as ``ProbeError``, never a partial map."""
    runner = _ScriptedRunner(
        routes={("git", "ls-remote", ORIGIN_URL): _failed(128, stderr="fatal")}
    )

    with pytest.raises(ProbeError):
        remote_ref_tips(runner, ORIGIN_URL)


# ---------------------------------------------------------------------------
# B. local_ref_map
# ---------------------------------------------------------------------------


def test_local_ref_map_parses_show_ref_output() -> None:
    """Show-ref ``sha SP ref`` lines become a ``{ref: sha}`` map."""
    runner = _ScriptedRunner(
        routes={
            ("git", "show-ref"): _ok(
                f"{MAIN_LOCAL} refs/heads/main\n"
                f"{TAG_V13} refs/tags/v1.3\n"
            ),
        }
    )

    result = local_ref_map(runner)

    assert result == {
        "refs/heads/main": MAIN_LOCAL,
        "refs/tags/v1.3": TAG_V13,
    }
    assert runner.argvs() == [["git", "show-ref"]]


def test_local_ref_map_passes_no_env() -> None:
    """Local-only probes run without the no-prompt ssh environment."""
    runner = _ScriptedRunner(routes={("git", "show-ref"): _ok("")})

    local_ref_map(runner)

    assert "env" not in runner.kwargs_for(["git", "show-ref"])


def test_local_ref_map_empty_when_no_refs() -> None:
    """Returncode 1 with empty stdout means an empty local map."""
    runner = _ScriptedRunner(
        routes={("git", "show-ref"): _failed(1, stdout="", stderr="")}
    )

    assert local_ref_map(runner) == {}


# ---------------------------------------------------------------------------
# C. ref_contains
# ---------------------------------------------------------------------------


def test_ref_contains_true_on_zero_return() -> None:
    """``merge-base --is-ancestor`` exit 0 means the tip is contained."""
    runner = _ScriptedRunner(
        routes={
            ("git", "merge-base", "--is-ancestor", MAIN_REMOTE, MAIN_LOCAL): _ok()
        }
    )

    assert ref_contains(runner, MAIN_REMOTE, MAIN_LOCAL) is True
    assert runner.argvs() == [
        ["git", "merge-base", "--is-ancestor", MAIN_REMOTE, MAIN_LOCAL]
    ]


def test_ref_contains_false_on_nonzero_return() -> None:
    """``merge-base --is-ancestor`` exit 1 means not contained."""
    runner = _ScriptedRunner(
        routes={
            (
                "git",
                "merge-base",
                "--is-ancestor",
                MAIN_REMOTE,
                MAIN_LOCAL,
            ): _failed(1)
        }
    )

    assert ref_contains(runner, MAIN_REMOTE, MAIN_LOCAL) is False


def test_ref_contains_passes_no_env() -> None:
    """Local containment checks run without the remote probe env."""
    runner = _ScriptedRunner(
        routes={
            ("git", "merge-base", "--is-ancestor", MAIN_REMOTE, MAIN_LOCAL): _ok()
        }
    )

    ref_contains(runner, MAIN_REMOTE, MAIN_LOCAL)

    kwargs = runner.kwargs_for(
        ["git", "merge-base", "--is-ancestor", MAIN_REMOTE, MAIN_LOCAL]
    )
    assert "env" not in kwargs


# ---------------------------------------------------------------------------
# D. check_freshness scenarios
# ---------------------------------------------------------------------------


def test_check_freshness_all_equal_is_clean() -> None:
    """Identical remote/local tips need no prompt and list nothing."""
    remote, local = _clean_lines()
    runner = _freshness_runner(remote, local)

    result = check_freshness(runner, ORIGIN_URL)

    assert result.behind == []
    assert result.divergent == []
    assert result.missing_tags == []
    assert result.moved_tags == []
    assert result.ahead == []
    assert result.local_only_branches == []
    assert result.local_only_tags == []
    assert result.probe_failed is False
    assert result.requires_prompt is False


def test_check_freshness_ahead_only_needs_no_prompt() -> None:
    """A remote tip that is an ancestor of the local tip is safe-ahead."""
    remote = [f"{MAIN_REMOTE}\trefs/heads/main"]
    local = [f"{MAIN_LOCAL} refs/heads/main"]
    runner = _freshness_runner(remote, local, merge_base_rc={MAIN_REMOTE: 0})

    result = check_freshness(runner, ORIGIN_URL)

    assert any("main" in entry for entry in result.ahead)
    assert result.requires_prompt is False
    assert result.behind == []
    assert result.divergent == []
    # The ancestor check ran through the injected runner.
    merge_calls = runner.merge_base_calls()
    assert len(merge_calls) == 1
    assert merge_calls[0][:4] == [
        "git",
        "merge-base",
        "--is-ancestor",
        MAIN_REMOTE,
    ]


def test_check_freshness_behind_when_remote_tip_unknown() -> None:
    """A remote tip present nowhere locally lands in ``behind``."""
    remote = [f"{FEATURE_REMOTE}\trefs/heads/main"]
    local = [f"{MAIN_LOCAL} refs/heads/main"]
    runner = _freshness_runner(
        remote, local, merge_base_rc={FEATURE_REMOTE: 1}
    )

    result = check_freshness(runner, ORIGIN_URL)

    assert any("main" in entry for entry in result.behind)
    assert result.requires_prompt is True


def test_check_freshness_divergent_when_tip_only_via_other_ref() -> None:
    """Fetched-but-unmerged tips are divergent, never safe-ahead.

    The remote tip object exists locally (via the remote-tracking
    ref) yet the same-name branch does not contain it, so bare
    object presence must not classify it as ahead.
    """
    remote = [f"{MAIN_REMOTE}\trefs/heads/main"]
    local = [
        f"{MAIN_LOCAL} refs/heads/main",
        f"{MAIN_REMOTE} refs/remotes/origin/main",
    ]
    runner = _freshness_runner(remote, local, merge_base_rc={MAIN_REMOTE: 1})

    result = check_freshness(runner, ORIGIN_URL)

    assert any("main" in entry for entry in result.divergent)
    assert result.ahead == []
    assert result.requires_prompt is True
    merge_calls = runner.merge_base_calls()
    assert len(merge_calls) == 1
    assert merge_calls[0][:4] == [
        "git",
        "merge-base",
        "--is-ancestor",
        MAIN_REMOTE,
    ]


def test_check_freshness_remote_only_branch_is_behind() -> None:
    """A remote branch with no local same-name ref lands in ``behind``."""
    remote = [
        f"{MAIN_REMOTE}\trefs/heads/main",
        f"{FEATURE_REMOTE}\trefs/heads/feature",
    ]
    local = [f"{MAIN_REMOTE} refs/heads/main"]
    runner = _freshness_runner(remote, local)

    result = check_freshness(runner, ORIGIN_URL)

    assert any("feature" in entry for entry in result.behind)
    assert result.requires_prompt is True


def test_check_freshness_moved_tag() -> None:
    """A same-name tag with a different sha lands in ``moved_tags``."""
    remote = [f"{TAG_V20_REMOTE}\trefs/tags/v2.0"]
    local = [f"{TAG_V20_LOCAL} refs/tags/v2.0"]
    runner = _freshness_runner(remote, local)

    result = check_freshness(runner, ORIGIN_URL)

    assert any("v2.0" in entry for entry in result.moved_tags)
    assert result.requires_prompt is True


def test_check_freshness_missing_remote_tag() -> None:
    """A remote tag absent locally lands in ``missing_tags``."""
    remote = [
        f"{MAIN_REMOTE}\trefs/heads/main",
        f"{TAG_V13}\trefs/tags/v1.3",
    ]
    local = [f"{MAIN_REMOTE} refs/heads/main"]
    runner = _freshness_runner(remote, local)

    result = check_freshness(runner, ORIGIN_URL)

    assert any("v1.3" in entry for entry in result.missing_tags)
    assert result.requires_prompt is True


def test_check_freshness_local_only_refs_announce_only() -> None:
    """Local branches/tags absent remotely never prompt, only announce."""
    remote = [f"{MAIN_REMOTE}\trefs/heads/main"]
    local = [
        f"{MAIN_REMOTE} refs/heads/main",
        f"{MAIN_LOCAL} refs/heads/secret-branch",
        f"{TAG_V13} refs/tags/local-tag",
    ]
    runner = _freshness_runner(remote, local)

    result = check_freshness(runner, ORIGIN_URL)

    assert any("secret-branch" in entry for entry in result.local_only_branches)
    assert any("local-tag" in entry for entry in result.local_only_tags)
    assert result.requires_prompt is False
    assert result.behind == []
    assert result.divergent == []
    assert result.missing_tags == []
    assert result.moved_tags == []
    assert result.ahead == [] or any(
        "secret-branch" not in entry for entry in result.ahead
    )


def test_check_freshness_probe_failure_continues_without_prompt() -> None:
    """A failed remote probe yields ``probe_failed`` and empty lists."""
    _, local = _clean_lines()
    runner = _freshness_runner(
        [], local, ls_remote_rc=128
    )

    result = check_freshness(runner, ORIGIN_URL)

    assert result.probe_failed is True
    assert result.behind == []
    assert result.divergent == []
    assert result.missing_tags == []
    assert result.moved_tags == []
    assert result.ahead == []
    assert result.local_only_branches == []
    assert result.local_only_tags == []
    assert result.requires_prompt is False


# ---------------------------------------------------------------------------
# E. format functions
# ---------------------------------------------------------------------------


def test_format_freshness_prompt_none_when_clean() -> None:
    """A clean result needs no consent prompt."""
    result = FreshnessResult(
        behind=[],
        divergent=[],
        missing_tags=[],
        moved_tags=[],
        ahead=[],
        local_only_branches=[],
        local_only_tags=[],
    )

    assert format_freshness_prompt(result) is None


def test_format_freshness_prompt_names_divergent_and_asks_consent() -> None:
    """The unified prompt names each divergent branch and its consequence."""
    result = FreshnessResult(
        behind=[],
        divergent=["main"],
        missing_tags=[],
        moved_tags=[],
        ahead=[],
        local_only_branches=[],
        local_only_tags=[],
    )

    prompt = format_freshness_prompt(result)

    assert prompt is not None
    assert "main" in prompt
    assert "migrate local state anyway?" in prompt


def test_format_freshness_prompt_joins_parts_with_consent_suffix() -> None:
    """Behind/missing/moved parts join with commas before the consent ask."""
    result = FreshnessResult(
        behind=["feature"],
        divergent=[],
        missing_tags=["v1.3"],
        moved_tags=["v2.0"],
        ahead=[],
        local_only_branches=[],
        local_only_tags=[],
    )

    prompt = format_freshness_prompt(result)

    assert prompt is not None
    assert "feature" in prompt
    assert "v1.3" in prompt
    assert "v2.0" in prompt
    assert "migrate local state anyway?" in prompt


def test_format_freshness_prompt_folds_local_only_with_separator() -> None:
    """Coexisting local-only refs fold into the same prompt after ``; ``."""
    result = FreshnessResult(
        behind=["feature"],
        divergent=[],
        missing_tags=[],
        moved_tags=[],
        ahead=[],
        local_only_branches=["secret-branch"],
        local_only_tags=["local-tag"],
    )

    prompt = format_freshness_prompt(result)

    assert prompt is not None
    assert "feature" in prompt
    assert "secret-branch" in prompt
    assert "local-tag" in prompt
    assert "; " in prompt
    assert "migrate local state anyway?" in prompt


def test_format_local_only_notice_none_when_empty() -> None:
    """No local-only refs means no announce notice."""
    result = FreshnessResult(
        behind=[],
        divergent=[],
        missing_tags=[],
        moved_tags=[],
        ahead=[],
        local_only_branches=[],
        local_only_tags=[],
    )

    assert format_local_only_notice(result) is None


def test_format_local_only_notice_lists_names() -> None:
    """The announce notice lists the branches and tags that will migrate."""
    result = FreshnessResult(
        behind=[],
        divergent=[],
        missing_tags=[],
        moved_tags=[],
        ahead=[],
        local_only_branches=["secret-branch"],
        local_only_tags=["local-tag"],
    )

    notice = format_local_only_notice(result)

    assert notice is not None
    assert "secret-branch" in notice
    assert "local-tag" in notice


# ---------------------------------------------------------------------------
# F. gate_local_source
# ---------------------------------------------------------------------------


def test_gate_deny_aborts_with_exit_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Declining the unified prompt aborts before anything mutates."""
    remote = [f"{FEATURE_REMOTE}\trefs/heads/main"]
    local = [f"{MAIN_LOCAL} refs/heads/main"]
    runner = _freshness_runner(
        remote, local, merge_base_rc={FEATURE_REMOTE: 1}
    )
    prompter = _ScriptedPrompter(response=False)

    with pytest.raises(SystemExit) as exc_info:
        gate_local_source(runner, _make_cwd(), prompter)

    assert exc_info.value.code == 1
    assert len(prompter.prompts) == 1
    assert "migrate local state anyway?" in prompter.prompts[0]
    out = capsys.readouterr().out
    assert out.startswith(
        f"Using local checkout {CWD_PATH} as clone source (origin {ORIGIN_URL})"
    )


def test_gate_accept_returns_result(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Accepting the unified prompt returns the freshness result."""
    remote = [f"{FEATURE_REMOTE}\trefs/heads/main"]
    local = [f"{MAIN_LOCAL} refs/heads/main"]
    runner = _freshness_runner(
        remote, local, merge_base_rc={FEATURE_REMOTE: 1}
    )
    prompter = _ScriptedPrompter(response=True)

    result = gate_local_source(runner, _make_cwd(), prompter)

    assert len(prompter.prompts) == 1
    assert result.requires_prompt is True
    out = capsys.readouterr().out
    assert out.startswith(
        f"Using local checkout {CWD_PATH} as clone source (origin {ORIGIN_URL})"
    )


def test_gate_non_tty_deny_aborts() -> None:
    """A non-tty EOF deny (prompter False) aborts like an explicit No."""
    remote = [f"{FEATURE_REMOTE}\trefs/heads/main"]
    local = [f"{MAIN_LOCAL} refs/heads/main"]
    runner = _freshness_runner(
        remote, local, merge_base_rc={FEATURE_REMOTE: 1}
    )

    with pytest.raises(SystemExit) as exc_info:
        gate_local_source(
            runner, _make_cwd(), _ScriptedPrompter(response=False)
        )

    assert exc_info.value.code == 1


def test_gate_announce_path_skips_prompter_and_prints_notice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ahead/local-only state announces without prompting."""
    remote = [f"{MAIN_REMOTE}\trefs/heads/main"]
    local = [
        f"{MAIN_LOCAL} refs/heads/main",
        f"{MAIN_LOCAL} refs/heads/secret-branch",
    ]
    runner = _freshness_runner(remote, local, merge_base_rc={MAIN_REMOTE: 0})
    prompter = _ScriptedPrompter(response=True)

    result = gate_local_source(runner, _make_cwd(), prompter)

    assert prompter.prompts == []
    assert result.requires_prompt is False
    out = capsys.readouterr().out
    assert out.startswith(
        f"Using local checkout {CWD_PATH} as clone source (origin {ORIGIN_URL})"
    )
    assert "secret-branch" in out


def test_gate_dirt_notice_present_when_dirty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Uncommitted changes print the informational dirt notice."""
    remote, local = _clean_lines()
    runner = _freshness_runner(remote, local, dirty=True)
    prompter = _ScriptedPrompter(response=True)

    gate_local_source(runner, _make_cwd(), prompter)

    out = capsys.readouterr().out
    assert "uncommitted changes present; only branches and tags migrate" in out
    assert prompter.prompts == []


def test_gate_dirt_notice_absent_when_clean(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A clean tree prints no dirt notice."""
    remote, local = _clean_lines()
    runner = _freshness_runner(remote, local, dirty=False)
    prompter = _ScriptedPrompter(response=True)

    gate_local_source(runner, _make_cwd(), prompter)

    out = capsys.readouterr().out
    assert "uncommitted changes" not in out


def test_gate_using_line_comes_before_dirt_notice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``Using local checkout`` line is first, dirt notice follows."""
    remote, local = _clean_lines()
    runner = _freshness_runner(remote, local, dirty=True)

    gate_local_source(runner, _make_cwd(), _ScriptedPrompter(response=True))

    out = capsys.readouterr().out
    using_line = (
        f"Using local checkout {CWD_PATH} as clone source (origin {ORIGIN_URL})"
    )
    assert out.startswith(using_line)
    assert out.index(using_line) < out.index("uncommitted changes present")


def test_gate_probe_failure_warns_on_stderr_and_proceeds(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Probe failure warns on stderr, never prompts, and returns the flag."""
    _, local = _clean_lines()
    runner = _freshness_runner([], local, ls_remote_rc=128)
    prompter = _ScriptedPrompter(response=True)

    result = gate_local_source(runner, _make_cwd(), prompter)

    assert result.probe_failed is True
    assert prompter.prompts == []
    captured = capsys.readouterr()
    assert captured.out.startswith(
        f"Using local checkout {CWD_PATH} as clone source (origin {ORIGIN_URL})"
    )
    assert "warning" in captured.err.lower()


# ---------------------------------------------------------------------------
# G. has_uncommitted_changes
# ---------------------------------------------------------------------------


def test_has_uncommitted_changes_true_when_output() -> None:
    """Non-empty ``status --porcelain`` output means dirty."""
    runner = _ScriptedRunner(
        routes={("git", "status", "--porcelain"): _ok(" M dirty.py\n")}
    )

    assert has_uncommitted_changes(runner) is True
    assert runner.argvs() == [["git", "status", "--porcelain"]]


def test_has_uncommitted_changes_false_when_empty() -> None:
    """Empty ``status --porcelain`` output means clean."""
    runner = _ScriptedRunner(
        routes={("git", "status", "--porcelain"): _ok("")}
    )

    assert has_uncommitted_changes(runner) is False


def test_has_uncommitted_changes_passes_no_env() -> None:
    """The dirt probe is local-only, so it sends no env."""
    runner = _ScriptedRunner(
        routes={("git", "status", "--porcelain"): _ok("")}
    )

    has_uncommitted_changes(runner)

    assert "env" not in runner.kwargs_for(["git", "status", "--porcelain"])
