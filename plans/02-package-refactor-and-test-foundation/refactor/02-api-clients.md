# Stage 02 — API clients (Codeberg and GitHub)

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** stage 01 (`MigrationState`, `StateLoadError`).
**Blocks:** stage 04 (orchestrator), stage 06 (CLI wiring).

## 1. Objective

Replace the module-level `cb_headers`, `gh_headers`, `gh_request`,
`fetch_codeberg_description`, `create_github_repo`,
`fetch_all_codeberg_issues`, `fetch_codeberg_comments`,
`create_github_issue`, `create_github_comment`, and `close_github_issue`
functions in `f2gh.py` with two classes:

- `forgejo_to_github.codeberg.CodebergClient` — wraps Forgejo v1 API
  for repo metadata, issues, and comments.
- `forgejo_to_github.github.GitHubClient` — wraps GitHub REST API v3
  for repo creation, issue/comment/label operations, and repository
  description updates.

Both clients accept a `Transport` Protocol so tests can supply a fake.
A default factory builds a `requests.Session`-backed adapter. The
clients own their base URLs, headers, and error translation; they do
not know about the orchestrator, the reporter, or the state store.

## 2. Files / modules

- **New module:** `forgejo_to_github/transport.py` containing:
  - `Transport` Protocol definition.
  - `RequestsTransport` default adapter (wraps `requests.Session`).
  - `Response` Protocol / structural shape used by tests' fake
    responses.

- **New module:** `forgejo_to_github/codeberg.py` containing:
  - `CodebergClient` class.
  - `CodebergError` base + `CodebergNotFoundError`,
    `CodebergAuthError`, `CodebergTransportError`,
    `CodebergRateLimitError`, `CodebergValidationError`.

- **New module:** `forgejo_to_github/github.py` containing:
  - `GitHubClient` class.
  - `GitHubError` base + `GitHubAuthError`, `GitHubValidationError`,
    `GitHubRateLimitError`, `GitHubTransportError`.

- `forgejo_to_github/__init__.py` is not modified during this stage.

## 3. Public API and responsibilities

### 3.1 Transport Protocol

```python
class Transport(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        json_body: Any | None = None,
        timeout: float | None = None,
    ) -> Response: ...
```

The Protocol is structural (duck-typed). It is satisfied by:

- `RequestsTransport` — the production default, wraps
  `requests.Session.request`. Forwards kwargs through.
- The test fixtures in `tests/test_codeberg_client.py` and
  `tests/test_github_client.py` (the local `FakeTransport`).

The Protocol does not require a `Response` type; clients read
`status_code`, `headers`, `url`, and call `json()` on the returned
object. The test fake provides those attributes directly.

### 3.2 `CodebergClient`

```python
CodebergClient(
    base_url: str,
    owner: str,
    repo: str,
    token: str | None,
    transport: Transport | None = None,
)
```

When `transport is None`, the constructor instantiates
`RequestsTransport()`. The default adapter must not be created at
module import time — only when the client is constructed and `transport`
is `None`.

Methods:

| Method | Returns | HTTP contract |
|--------|---------|---------------|
| `list_issues(state: str = "all") -> Iterator[dict]` | generator of parsed issue dicts | `GET /repos/{owner}/{repo}/issues?state={state}&type=issues&page=N&limit=50`; paginates until an empty page is returned. Sorts results by `created_at` ascending on finalization (or returns a list sorted — pick one and document). The test `test_fetch_all_codeberg_issues_sorts_by_created_at_ascending` implies sort; choose to return `list[dict]` rather than `Iterator[dict]` so the test pattern `list(client.list_issues())` plus sorting is supported. **Decision: `list_issues` returns a `list[dict]`**, sorted ascending by `created_at`. |
| `list_comments(issue_id: int) -> Iterator[dict]` | generator | `GET /repos/{owner}/{repo}/issues/{issue_id}/comments?issue_id={issue_id}&page=N`; paginates until empty page. **Decision: `list_comments` returns `list[dict]`.** |
| `get_issue(issue_number: int) -> dict` | parsed dict | `GET /repos/{owner}/{repo}/issues/{issue_number}` |
| `get_repository_description() -> str` | description string | `GET /repos/{owner}/{repo}`; returns the `description` field, or empty string if missing/null |

Error translation rules:

| Status / condition | Translated to |
|--------------------|---------------|
| 200 with valid payload | normal return |
| 404 | `CodebergNotFoundError` carrying `issue_number` (when applicable) and `url` |
| 401, 403 | `CodebergAuthError` |
| 429 | `CodebergRateLimitError` with `retry_after: int | None` |
| 5xx (after no internal retry; Codeberg clients do not retry) | `CodebergTransportError` |
| Underlying transport raises (connection refused, DNS failure, timeout) | `CodebergTransportError`; the message must not contain the token. |

Headers:

- `Accept: application/json` always.
- `User-Agent: forgejo-to-github/<version>` always. (The version is
  read from the package metadata or hard-coded to `"forgejo-to-github"`
  if not available.)
- `Authorization: token <CODEBERG_TOKEN>` only when `token` is not
  `None`. When `token is None`, the `Authorization` header must be
  absent entirely (not present-but-empty).

### 3.3 `GitHubClient`

```python
GitHubClient(
    base_url: str,
    owner: str,
    repo: str,
    token: str | None,
    transport: Transport | None = None,
)
```

Methods:

| Method | Returns | HTTP contract |
|--------|---------|---------------|
| `create_repository(name: str, description: str | None, public: bool) -> dict` | parsed repo dict | `POST /user/repos` with payload `{name, private: not public, description, has_issues: True}`. On non-2xx, falls back to `POST /orgs/{owner}/repos` and retries (only the owner-fallback path is in scope for the org endpoint). |
| `update_repository_description(description: str) -> None` | None | `PATCH /repos/{owner}/{repo}` with `{"description": description}`. Called only when a non-empty description is provided; the orchestrator is responsible for the empty-description skip. |
| `check_repository_exists() -> dict | None` | parsed repo dict on 200, `None` on 404 | `GET /repos/{owner}/{repo}` |
| `create_issue(title: str, body: str, labels: list[str]) -> int` | issue `number` | `POST /repos/{owner}/{repo}/issues` with `{title, body, labels}`; returns parsed `number` |
| `create_comment(issue_number: int, body: str) -> int` | comment `id` | `POST /repos/{owner}/{repo}/issues/{issue_number}/comments` with `{body}`; returns parsed `id` |
| `close_issue(issue_number: int) -> None` | None | `PATCH /repos/{owner}/{repo}/issues/{issue_number}` with `{state: "closed"}` |
| `ensure_label(name: str, color: str, description: str) -> None` | None | `GET /repos/{owner}/{repo}/labels/{name}` first; on 404, `POST /repos/{owner}/{repo}/labels` with `{name, color, description}`. On 200 from the GET, no POST is issued. |

Error translation rules:

| Status / condition | Translated to |
|--------------------|---------------|
| 2xx with valid payload | normal return |
| 401, 403 | `GitHubAuthError` |
| 422 | `GitHubValidationError` carrying parsed `errors` from the response body |
| 403 with `X-RateLimit-Remaining: 0` | `GitHubRateLimitError` carrying `reset: int` |
| 429 | `GitHubRateLimitError` with `retry_after: int | None` |
| Secondary rate limit (three consecutive 429 or 403-with-zero-remaining responses) | after three retries, raise `GitHubRateLimitError` |
| 5xx (no internal retry beyond secondary-rate-limit logic) | `GitHubTransportError` |
| Underlying transport raises | `GitHubTransportError` |

The retry/backoff behavior for 429 / 403-with-zero-remaining lives
inside the GitHub client, not the orchestrator. After three attempts,
the client raises `GitHubRateLimitError`. The test
`test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error`
asserts that exactly three POST attempts are issued before the client
gives up.

Headers (production default adapter only; the test fake observes
whatever headers the client constructs):

- `Authorization: Bearer <GITHUB_TOKEN>` only when `token` is not
  `None`.
- `Accept: application/vnd.github+json`.
- `X-GitHub-Api-Version: 2022-11-28`.

### 3.4 Error type hierarchy

Each API module defines its own error hierarchy rooted in its own base
exception class. The base class names are deliberately distinct
(`CodebergError` vs `GitHubError`) so callers can `except` either
without ambiguity.

```
CodebergError(Exception)
├── CodebergNotFoundError  (attributes: issue_number: int | None, url: str)
├── CodebergAuthError
├── CodebergTransportError
├── CodebergRateLimitError  (attributes: retry_after: int | None)
└── CodebergValidationError  (attributes: messages: list[str])

GitHubError(Exception)
├── GitHubAuthError
├── GitHubValidationError  (attributes: messages: list[str])
├── GitHubRateLimitError   (attributes: reset: int | None, retry_after: int | None)
└── GitHubTransportError
```

`CodebergNotFoundError` is imported and used in `test_codeberg_client.py::test_get_issue_404_raises_not_found_with_context`
and is asserted to carry `err.issue_number == 99` and `err.url.endswith("/issues/99")`.

## 4. Invariants

- **No I/O at module import.** `forgejo_to_github.transport`,
  `forgejo_to_github.codeberg`, and `forgejo_to_github.github` must
  not perform network or filesystem work when imported. The
  `test_importing_package_does_not_perform_network_calls` and
  `test_importing_package_does_not_execute_subprocess` tests assert
  this.
- **Transport is always injectable.** No client has a hard-coded
  default for `transport=` other than `None` (the sentinel for
  "construct the production adapter at instance time"). Tests pass an
  explicit fake.
- **Token redaction on every error path.** Every exception message and
  every `str()` representation of a client must not contain the
  client's token. This is asserted by
  `test_transport_error_does_not_leak_token` (Codeberg) and by the
  redaction discipline implicit in
  `test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error`
  and `test_403_with_zero_rate_limit_remaining_raises_rate_limit_error`
  (GitHub). The implementation must apply redaction before raising.
- **No dependency on the orchestrator or reporter.** The clients know
  nothing about `MigrationOrchestrator`, `Reporter`, or `StateStore`.

## 5. Collaborator / dependency rules

- `CodebergClient` and `GitHubClient` depend on `Transport`. They do
  not depend on each other.
- `Transport` Protocol depends on nothing.
- `RequestsTransport` depends on `requests` (already a project
  dependency).
- `forgejo_to_github.codeberg` and `forgejo_to_github.github` may
  import from `forgejo_to_github.transport` and `forgejo_to_github.domain`
  (for shared domain types if any), but neither imports from the
  other.

## 6. Migration / compatibility constraints

- **`f2gh.py` is not modified in this stage.** The legacy module-level
  functions stay in place; tests in `tests/test_api_clients.py` and
  `tests/test_repository_description.py` continue to pass against the
  legacy functions. Removal happens in stage 06.
- **Public test surface is the new clients.** The tests in
  `tests/test_codeberg_client.py` and `tests/test_github_client.py`
  exercise the new public surface. They are the contract.
- **Payload shape preserved.** Where a test asserts a specific JSON
  payload (e.g., `test_create_issue_posts_expected_payload_and_returns_number`
  asserts `{title, body, labels}`), the new client must produce the
  exact same payload. No silent renames of fields.
- **Endpoint paths preserved.** No consolidation of endpoints, no
  changing `/user/repos` to a different route, no changing `/repos/.../issues`
  to `/repos/.../issues/` (trailing slash). The existing tests pin the
  paths.

## 7. Test references

Codeberg:

- `tests/test_codeberg_client.py::test_list_issues_paginates_until_empty_page`
- `tests/test_codeberg_client.py::test_list_issues_sends_expected_request_params`
- `tests/test_codeberg_client.py::test_list_issues_sets_json_accept_and_user_agent`
- `tests/test_codeberg_client.py::test_list_issues_omits_auth_header_when_no_token`
- `tests/test_codeberg_client.py::test_list_issues_sends_token_authorization_when_configured`
- `tests/test_codeberg_client.py::test_list_comments_passes_issue_id_param_and_paginates`
- `tests/test_codeberg_client.py::test_get_issue_returns_parsed_payload`
- `tests/test_codeberg_client.py::test_get_issue_404_raises_not_found_with_context`
- `tests/test_codeberg_client.py::test_get_issue_auth_errors_raise_codeberg_auth_error`
- `tests/test_codeberg_client.py::test_transport_error_translates_to_codeberg_transport_error`
- `tests/test_codeberg_client.py::test_transport_error_does_not_leak_token`
- `tests/test_codeberg_client.py::test_429_translates_to_rate_limit_error_with_retry_after`
- `tests/test_codeberg_client.py::test_429_without_retry_after_header_still_raises_rate_limit_error`

GitHub:

- `tests/test_github_client.py::test_create_repository_private_posts_expected_payload`
- `tests/test_github_client.py::test_create_repository_public_posts_private_false`
- `tests/test_github_client.py::test_create_repository_includes_description_when_provided`
- `tests/test_github_client.py::test_create_issue_posts_expected_payload_and_returns_number`
- `tests/test_github_client.py::test_create_comment_posts_body_and_returns_id`
- `tests/test_github_client.py::test_close_issue_patches_state_closed`
- `tests/test_github_client.py::test_ensure_label_posts_payload_when_label_missing`
- `tests/test_github_client.py::test_ensure_label_does_not_repost_when_label_already_exists`
- `tests/test_github_client.py::test_create_issue_422_raises_validation_error_with_messages`
- `tests/test_github_client.py::test_create_issue_auth_errors_raise_github_auth_error`
- `tests/test_github_client.py::test_403_with_zero_rate_limit_remaining_raises_rate_limit_error`
- `tests/test_github_client.py::test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error`

Package boundary:

- `tests/test_package_boundaries.py::test_intended_public_class_is_importable`
  (parameterized for both `forgejo_to_github.codeberg.CodebergClient`
  and `forgejo_to_github.github.GitHubClient`)
- `tests/test_package_boundaries.py::test_public_class_has_docstring`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_least_two_public_methods`
  (same)
- `tests/test_package_boundaries.py::test_public_class_has_at_most_seven_public_methods`
  (same)

Legacy parity (must remain green throughout this stage):

- All functions in `tests/test_api_clients.py`.
- All functions in `tests/test_repository_description.py`.

## 8. Implementation order

1. Add `forgejo_to_github/transport.py` with `Transport` Protocol and
   `RequestsTransport` default adapter.
2. Add `forgejo_to_github/codeberg.py` with the client and error
   hierarchy.
3. Run `./scripts/run-tests.sh tests/test_codeberg_client.py
   tests/test_package_boundaries.py`. Confirm green.
4. Add `forgejo_to_github/github.py` with the client and error
   hierarchy.
5. Run `./scripts/run-tests.sh tests/test_github_client.py
   tests/test_package_boundaries.py`. Confirm green.
6. Run the full suite via `./scripts/run-tests.sh`. All pre-existing
   tests must remain green.
7. Stop and report.

## 9. Verification commands

```bash
./scripts/run-tests.sh tests/test_codeberg_client.py
./scripts/run-tests.sh tests/test_github_client.py
./scripts/run-tests.sh tests/test_package_boundaries.py
./scripts/run-tests.sh tests/test_api_clients.py
./scripts/run-tests.sh tests/test_repository_description.py
./scripts/run-tests.sh                          # full suite
ruff check forgejo_to_github/transport.py forgejo_to_github/codeberg.py forgejo_to_github/github.py
```

`mypy` on the new modules is informational.

## 10. Stop gate

The implementing agent stops and reports:

- Confirmation that `forgejo_to_github.transport`,
  `forgejo_to_github.codeberg`, and `forgejo_to_github.github` exist
  and meet the public surface in this spec.
- Test results for the four targeted suites plus the full suite.
- Any deviation from the locked method signatures, even in argument
  order, with justification.
- Confirmation that no `f2gh.py` symbols were modified in this stage.

The user reviews before stage 03 begins.

## 11. Out of scope

- Editing `f2gh.py` to call the new clients. That is stage 06.
- The orchestrator. That is stage 04.
- Subclassing `requests.Session` directly inside the clients. The
  default adapter is `RequestsTransport`, not a `Session` subclass.
- A real async/await transport. The Protocol is synchronous; the
  production adapter is `requests`-based.
- Adding label-color defaulting logic. The orchestrator passes the
  color; the client forwards it. (A documented default color is
  asserted by `test-framework-spec.md` §10.3 to be applied by the
  orchestrator when missing — that orchestration concern is stage 04.)
