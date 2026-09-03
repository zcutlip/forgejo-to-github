# RED class: C. Integration
"""
Concrete integration tests added after audit to catch collaborator wiring mismatches.

This module was added after an audit revealed that unit tests using fakes
masked real signature mismatches between MigrationOrchestrator and its
concrete collaborators (CodebergClient, GitHubClient, GitMirror,
StateStore, Reporter). It wires the *real* classes together with only
their I/O boundaries faked (HTTP transport, git command runner/tempdir/
cleanup, output sinks) and asserts the integrated contract.

Single over-bundled test was refactored into three focused tests, each
with one primary reason to fail:

- test_git_wiring_lifecycle_orders_clone_then_push_branches_then_push_tags_then_cleanup
  wires real MigrationOrchestrator + real GitMirror and asserts
  clone → push_branches(--all) → push_tags(--tags) → cleanup ordering.
  Uses fake Codeberg/GitHub/state/reporter seams sufficient to reach/
  complete the Git phase, matching the concrete orchestrator API after
  implementation intent. Currently RED on orchestrator/GitMirror
  method mismatch (run_clone/run_push vs clone/push_branches/push_tags).

- test_github_wiring_creates_issue_and_comment_with_concrete_signatures
  wires real MigrationOrchestrator + real GitHubClient, fake Git seam
  reports success and fake Codeberg returns one issue with one comment;
  asserts GitHub receives concrete signatures/payloads
  (title, body, labels) and comment targets returned GitHub issue number.
  Currently RED on orchestrator/GitHub signature mismatch
  (dict payload vs concrete (title, body, labels)), not masked by Git.

- test_state_wiring_persists_successful_issue_to_state_json
  wires real MigrationOrchestrator + real StateStore, fake Git and API
  seams matching concrete interfaces; asserts successful issue is
  persisted to state.json and reloadable via StateStore.load().
  Currently RED on missing StateStore checkpoint/save integration
  (already_migrated/record_issue vs load/save), not masked by
  Git/GitHub failures.

If any collaborator method was renamed, re-typed, or re-scoped
(e.g. GitMirror.clone vs run_clone, GitHubClient.create_issue(title, body, labels)
vs create_issue(payload dict), StateStore.load/save vs already_migrated/record_issue),
the relevant test must fail with AttributeError/TypeError or a concrete assertion
failure — they intentionally do not accommodate the current broken wiring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forgejo_to_github.domain import Repository
from forgejo_to_github.git import GitMirror
from forgejo_to_github.github import GitHubClient
from forgejo_to_github.migration import MigrationOrchestrator
from forgejo_to_github.reporting import Reporter
from forgejo_to_github.state import StateStore

# ---------------------------------------------------------------------------
# Minimal fake HTTP transport/response (supports both clients)
# ---------------------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int
    json_payload: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    def json(self) -> Any:
        if self.json_payload is None:
            raise ValueError("no json body")
        return self.json_payload


@dataclass
class FakeRequest:
    method: str
    url: str
    params: dict[str, Any] | None = None
    headers: dict[str, str] | None = None
    json_body: Any = None


class FakeTransport:
    """Recording transport with scripted queue; supports params/headers/json_body."""

    def __init__(self, responses: list[FakeResponse | Exception] | None = None) -> None:
        self._scripted: list[FakeResponse | Exception] = list(responses or [])
        self.calls: list[FakeRequest] = []

    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        timeout: float | None = None,
    ) -> FakeResponse:
        self.calls.append(
            FakeRequest(
                method=method,
                url=url,
                params=params,
                headers=headers,
                json_body=json_body,
            )
        )
        if not self._scripted:
            raise AssertionError(
                f"FakeTransport: no scripted response for {method} {url} (call #{len(self.calls)})"
            )
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# ---------------------------------------------------------------------------
# Git fakes (command runner / tempdir / cleanup)
# ---------------------------------------------------------------------------


class _FakeCompletedProcess:
    def __init__(
        self, args: list[str], returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> None:
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(
        self,
        args: list[str],
        *,
        check: bool = False,
        capture_output: bool = False,
        text: bool = False,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> _FakeCompletedProcess:
        self.calls.append(list(args))
        return _FakeCompletedProcess(
            args=list(args), returncode=0, stdout="", stderr=""
        )


class RecordingSink:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, line: str) -> None:
        self.lines.append(line)


# ---------------------------------------------------------------------------
# Private fixtures / builders (avoid repeating huge setup in each test)
# ---------------------------------------------------------------------------


def _make_repo(*, dry_run: bool = False, skip_git: bool = False) -> Repository:
    return Repository(
        source="owner/source", target="owner/target", dry_run=dry_run, skip_git=skip_git
    )


def _make_recording_reporter() -> tuple[Reporter, RecordingSink, RecordingSink]:
    out = RecordingSink()
    err = RecordingSink()
    return Reporter(output=out, error_output=err), out, err


def _build_real_git_mirror(
    tmp_path: Path,
) -> tuple[GitMirror, FakeRunner, list[str], list[str]]:
    runner = FakeRunner()
    cleanup_calls: list[str] = []
    created_dirs: list[str] = []

    def fake_cleanup(path: str, *args: Any, **kwargs: Any) -> None:
        cleanup_calls.append(path)

    def fake_tempdir_factory(prefix: str | None = None, **kwargs: Any) -> str:
        d = tmp_path / f"{prefix or 'f2gh'}-mirror"
        d.mkdir(parents=True, exist_ok=True)
        created_dirs.append(str(d))
        return str(d)

    git = GitMirror(
        source_url="https://codeberg.org/owner/source.git",
        target_url="https://github.com/owner/target.git",
        github_token="gh-token",
        command_runner=runner,
        tempdir_factory=fake_tempdir_factory,
        cleanup=fake_cleanup,
    )
    return git, runner, cleanup_calls, created_dirs


# -- Fake collaborators sufficient for Git wiring test (reach Git phase) --


class _FakeCodebergEmpty:
    def list_issues(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return []

    def list_comments(
        self, issue_id: int, *args: Any, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return []

    def get_repository_description(self, *args: Any, **kwargs: Any) -> str:
        return ""


class _FakeGitHubEmpty:
    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        return 999

    def create_comment(self, issue_number: int, body: str) -> int:
        return 1

    def close_issue(self, issue_number: int) -> None:
        return None

    def check_repository_exists(self) -> dict[str, Any] | None:
        return {"exists": True}

    def create_repository(
        self, name: str, description: str | None, public: bool
    ) -> dict[str, Any]:
        return {}

    def update_repository_description(self, description: str) -> None:
        return None

    def ensure_label(self, name: str, color: str, description: str) -> None:
        return None


class _FakeStateForGitAndGithub:
    """Minimal state seam for tests that isolate non-state behavior."""

    def __init__(self) -> None:
        self.recorded: dict[int, int] = {}

    def already_migrated(self, source_number: int) -> bool:
        return source_number in self.recorded

    def record_issue(self, source_number: int, github_number: int) -> None:
        self.recorded[source_number] = github_number

    def record_comment(
        self, source_number: int, comment_index: int, github_comment_id: int
    ) -> None:
        return None


# -- Fake Git that reports success (for GitHub and State tests) --


class _FakeGitSuccess:
    """Fake Git seam used when a test skips the Git phase."""

    def __init__(self) -> None:
        self.clone_calls: list[str] = []
        self.push_branches_calls: list[str] = []
        self.push_tags_calls: list[str] = []
        self.cleanup_calls: list[str] = []

    def clone(self) -> str:
        self.clone_calls.append("clone")
        return "/tmp/fake-mirror"

    def push_branches(self, local_path: str) -> None:
        self.push_branches_calls.append(local_path)

    def push_tags(self, local_path: str) -> None:
        self.push_tags_calls.append(local_path)

    def cleanup(self, local_path: str) -> None:
        self.cleanup_calls.append(local_path)


# -- Fake Codeberg returning one issue with one comment (GitHub wiring) --


def _make_codeberg_one_issue() -> Any:
    issue = {
        "number": 1,
        "title": "Hello world",
        "body": "Issue body",
        "labels": ["bug", "enhancement"],
        "comments": [{"index": 0, "body": "Nice comment"}],
        "state": "open",
        "closed": False,
    }

    class _FakeCodebergOneIssue:
        def list_issues(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [dict(issue)]

        def list_comments(
            self, issue_id: int, *args: Any, **kwargs: Any
        ) -> list[dict[str, Any]]:
            if issue_id == 1:
                return [{"index": 0, "body": "Nice comment"}]
            return []

        def get_repository_description(self, *args: Any, **kwargs: Any) -> str:
            return "source description"

    return _FakeCodebergOneIssue()


# -- Dual-compatible fake GitHub for State wiring (handles old orchestrator payload) --


class _FakeGitHubForState:
    def __init__(self) -> None:
        self.issue_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.comment_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        self.issue_calls.append(((title, body, labels), {}))
        return 101

    def create_comment(self, issue_number: int, body: str) -> int:
        self.comment_calls.append(((issue_number, body), {}))
        return 9001

    def close_issue(self, issue_number: int) -> None:
        return None

    def check_repository_exists(self) -> None:
        return None

    def create_repository(
        self, name: str, description: str | None, public: bool
    ) -> dict[str, Any]:
        return {}

    def update_repository_description(self, description: str) -> None:
        return None

    def ensure_label(self, name: str, color: str, description: str) -> None:
        return None


# ---------------------------------------------------------------------------
# Focused integration tests (one primary reason to fail each)
# ---------------------------------------------------------------------------


def test_git_wiring_lifecycle_orders_clone_then_push_branches_then_push_tags_then_cleanup(
    tmp_path: Path,
) -> None:
    """
    Git wiring/lifecycle: real orchestrator + real GitMirror.
    Asserts clone → push_branches(--all) → push_tags(--tags) → cleanup ordering.
    Uses fake Codeberg/GitHub/state/reporter seams sufficient to reach/complete
    the Git phase, matching concrete orchestrator API after implementation intent.
    Currently RED on orchestrator/GitMirror method mismatch
    (run_clone/run_push vs clone/push_branches/push_tags/cleanup).
    """
    repo = _make_repo(dry_run=False, skip_git=False)
    state = _FakeStateForGitAndGithub()
    codeberg = _FakeCodebergEmpty()
    github = _FakeGitHubEmpty()
    git, runner, cleanup_calls, created_dirs = _build_real_git_mirror(tmp_path)
    reporter, _out, _err = _make_recording_reporter()

    orchestrator = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
    )

    # Must not raise AttributeError/TypeError after wiring fix; currently does.
    result = orchestrator.run()

    # --- Assert: Git clone then branch/tags push then cleanup ---
    assert runner.calls, "Git runner was never invoked"
    clone_indices = [i for i, c in enumerate(runner.calls) if "clone" in c]
    push_all_indices = [i for i, c in enumerate(runner.calls) if "--all" in c]
    push_tags_indices = [i for i, c in enumerate(runner.calls) if "--tags" in c]
    assert clone_indices, "Git clone was not invoked (expected 'clone' in argv)"
    assert push_all_indices, "Git push --all was not invoked"
    assert push_tags_indices, "Git push --tags was not invoked"
    assert min(clone_indices) < min(push_all_indices) < min(push_tags_indices), (
        f"Git phase ordering broken: clone {clone_indices}, --all {push_all_indices}, --tags {push_tags_indices}"
    )
    assert cleanup_calls, "Git cleanup was not invoked"
    assert created_dirs, "tempdir_factory was not invoked"
    assert cleanup_calls[0] == created_dirs[0], (
        f"cleanup path {cleanup_calls[0]!r} != clone path {created_dirs[0]!r}"
    )
    clone_argv = runner.calls[clone_indices[0]]
    assert "https://codeberg.org/owner/source.git" in clone_argv
    assert created_dirs[0] in clone_argv

    # Git status in result should reflect ok (or at least not skipped/failed)
    git_status = getattr(result, "git", {})
    assert git_status.get("clone") == "ok", f"git clone status {git_status!r}"
    assert git_status.get("push") == "ok", f"git push status {git_status!r}"


def test_github_wiring_creates_issue_and_comment_with_concrete_signatures(
    tmp_path: Path,
) -> None:
    """
    GitHub wiring: real orchestrator + real GitHubClient, fake Git seam
    reports success and fake Codeberg returns one issue with one comment;
    asserts GitHub receives concrete signatures/payloads and comment
    targets returned GitHub issue number.
    Currently RED on orchestrator/GitHub signature mismatch
    (payload dict vs (title, body, labels)), not masked by Git failure.
    """
    repo = _make_repo(dry_run=False, skip_git=True)

    # Real GitHubClient with scripted transport: successful issue + comment
    github_number = 101
    github_comment_id = 9001
    github_transport = FakeTransport(
        responses=[
            FakeResponse(status_code=201, json_payload={"number": github_number}),
            FakeResponse(status_code=201, json_payload={"id": github_comment_id}),
        ]
    )
    github = GitHubClient(
        base_url="https://api.github.com",
        owner="owner",
        repo="target",
        token="gh-token",
        transport=github_transport,
    )

    # Fake Git seam reports success (dual-compatible so Git phase not masked)
    git = _FakeGitSuccess()

    # Fake Codeberg returns one issue with one comment
    codeberg = _make_codeberg_one_issue()

    # Fake state (dual-compatible, already empty)
    state = _FakeStateForGitAndGithub()

    reporter, _out, _err = _make_recording_reporter()

    orchestrator = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
    )

    # Act: must not raise TypeError after wiring fix; currently does due to
    # orchestrator calling create_issue(dict) vs concrete (title, body, labels)
    result = orchestrator.run()

    # --- Assert: GitHub receives title/body/labels and GitHub issue number for comment ---
    assert len(github_transport.calls) >= 2, (
        f"expected issue+comment POSTs, got {github_transport.calls!r}"
    )
    issue_call = github_transport.calls[0]
    assert issue_call.method == "POST"
    assert issue_call.url == "https://api.github.com/repos/owner/target/issues"
    assert isinstance(issue_call.json_body, dict), (
        f"issue json_body not a dict: {issue_call.json_body!r}"
    )
    assert issue_call.json_body.get("title") == "Hello world", (
        f"title not forwarded: {issue_call.json_body!r}"
    )
    assert issue_call.json_body.get("body") == "Issue body", (
        f"body not forwarded: {issue_call.json_body!r}"
    )
    assert issue_call.json_body.get("labels") == ["bug", "enhancement"], (
        f"labels not forwarded: {issue_call.json_body!r}"
    )

    comment_call = github_transport.calls[1]
    assert comment_call.method == "POST"
    assert (
        comment_call.url
        == f"https://api.github.com/repos/owner/target/issues/{github_number}/comments"
    ), (
        f"comment posted to wrong issue URL (expected GitHub number {github_number}): {comment_call.url!r}"
    )
    assert isinstance(comment_call.json_body, dict)
    assert comment_call.json_body.get("body") == "Nice comment"

    # Also assert result counters reflect success (not failed due to signature)
    assert result.issues_attempted == 1, f"issues_attempted {result.issues_attempted!r}"
    assert result.issues_succeeded == 1, f"issues_succeeded {result.issues_succeeded!r}"
    assert result.issues_failed == 0
    assert result.comments_attempted == 1
    assert result.comments_succeeded == 1
    assert result.comments_failed == 0
    assert result.failures == []


def test_state_wiring_persists_successful_issue_to_state_json(tmp_path: Path) -> None:
    """
    State wiring: real orchestrator + real StateStore, fake Git and API
    seams matching concrete interfaces; asserts successful issue is
    persisted to state.json and reloadable.
    Currently RED on missing StateStore checkpoint/save integration
    (already_migrated/record_issue vs load/save), not masked by
    Git/GitHub failures.
    """
    repo = _make_repo(dry_run=False, skip_git=True)
    state_path = tmp_path / "state.json"
    state = StateStore(state_path, source="owner/source", target="owner/target")

    git = _FakeGitSuccess()

    # Codeberg returns one open issue with no comments (minimal for checkpoint)
    codeberg_issue = {
        "number": 1,
        "title": "Hello world",
        "body": "Issue body",
        "labels": ["bug"],
        "comments": [],
        "state": "open",
        "closed": False,
    }

    class _FakeCodebergForState:
        def list_issues(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            return [dict(codeberg_issue)]

        def list_comments(
            self, issue_id: int, *args: Any, **kwargs: Any
        ) -> list[dict[str, Any]]:
            return []

        def get_repository_description(self, *args: Any, **kwargs: Any) -> str:
            return ""

    codeberg = _FakeCodebergForState()
    github = _FakeGitHubForState()
    reporter, _out, _err = _make_recording_reporter()

    orchestrator = MigrationOrchestrator(
        repo=repo,
        codeberg=codeberg,
        github=github,
        git=git,
        state=state,
        reporter=reporter,
    )

    result = orchestrator.run()

    # --- Assert: checkpointed and persisted in state.json ---
    assert state_path.exists(), "state.json was not created"
    on_disk = json.loads(state_path.read_text())
    assert on_disk.get("migrated") == {"1": 101}, f"on-disk migrated wrong: {on_disk!r}"
    reloaded = state.load()
    # StateStore.load returns dict with int keys in current concrete API
    assert reloaded["migrated"] == {1: 101}, f"reloaded migrated wrong: {reloaded!r}"

    # --- Assert: result counters ---
    assert result.issues_attempted == 1
    assert result.issues_succeeded == 1
    assert result.issues_failed == 0
    assert result.failures == []
