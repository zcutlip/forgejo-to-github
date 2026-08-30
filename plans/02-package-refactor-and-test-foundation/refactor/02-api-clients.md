# Stage 02 — API clients (Codeberg and GitHub)

**Parent stage:** [`00-index.md`](./00-index.md)
**Depends on:** stage 01 (no direct dependency on state, but stage 04
will reference `MigrationState` shape).
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
  `requests.Session.request`. Forwards kwargs through. The adapter
  imports `requests` lazily inside its methods, not at module scope.
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
| `list_issues(state: str = "all") -> list[dict]` | list of parsed issue dicts sorted ascending by `created_at` | `GET /repos/{owner}/{repo}/issues?state={state}&type=issues&page=N&limit=50`; paginates until an empty page is returned. |
| `list_comments(issue_id: int) -> list[dict]` | list of parsed comment dicts in API order (chronological) | `GET /repos/{owner}/{repo}/issues/{issue_id}/comments?issue_id={issue_id}&page=N`; paginates until empty page. |
| `get_issue(issue_number: int) -> dict` | parsed dict | `GET /repos/{owner}/{repo}/issues/{issue_number}` |
| `get_repository_description() -> str` | description string. **Empty string** when the field is missing or `null`. The orchestrator is responsible for the "Migrated from Codeberg" fallback; the client does not invent a default. | `GET /repos/{owner}/{repo}`; returns the `description` field, or `""` if missing/null. |

Error translation rules. The order of these rules is the order the
client applies them:

| # | Status / condition | Translated to |
|---|--------------------|---------------|
| 1 | 200 with valid payload | normal return |
| 2 | 404 | `CodebergNotFoundError` carrying `issue_number` (when applicable) and `url` |
| 3 | 401, 403 | `CodebergAuthError` |
| 4 | 422 | `CodebergValidationError` carrying parsed error messages |
| 5 | 429 | `CodebergRateLimitError` with `retry_after: int | None` |
| 6 | 5xx | `CodebergTransportError` |
| 7 | Underlying transport raises (connection refused, DNS failure, timeout) | `CodebergTransportError`; the message must not contain the token. |

The 403-vs-429 distinction in Codeberg is simple: any 403 maps to
`CodebergAuthError`. There is no "X-RateLimit-Remaining: 0" rule
because the Forgejo v1 API does not emit that header for primary rate
limits (it uses 429).

Headers:

- `Accept: application/json` always.
- `User-Agent: forgejo-to-github/<version>` always. The version is
  read from the package metadata or hard-coded to `"forgejo-to-github"`
  if not available.
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
| `create_repository(name: str, description: str | None, public: bool) -> dict` | parsed repo dict | `POST /user/repos` with payload `{name, private: not public, description, has_issues: True}`. On non-2xx, falls back to `POST /orgs/{owner}/repos` and retries (only the owner-fallback path is in scope for the org endpoint). The personal-then-org order is locked. |
| `update_repository_description(description: str) -> None` | None | `PATCH /repos/{owner}/{repo}` with `{"description": description}`. The orchestrator is responsible for the empty-description skip; the client issues the call whenever it is invoked. |
| `check_repository_exists() -> dict | None` | parsed repo dict on 200, `None` on 404 | `GET /repos/{owner}/{repo}` |
| `create_issue(title: str, body: str, labels: list[str]) -> int` | issue `number` | `POST /repos/{owner}/{repo}/issues` with `{title, body, labels}`; returns parsed `number` |
| `create_comment(issue_number: int, body: str) -> int` | comment `id` | `POST /repos/{owner}/{repo}/issues/{issue_number}/comments` with `{body}`; returns parsed `id` |
| `close_issue(issue_number: int) -> None` | None | `PATCH /repos/{owner}/{repo}/issues/{issue_number}` with `{state: "closed"}` |
| `ensure_label(name: str, color: str, description: str) -> None` | None | `GET /repos/{owner}/{repo}/labels/{name}` first; on 404, `POST /repos/{owner}/{repo}/labels` with `{name, color, description}`. On 200 from the GET, no POST is issued. |

Error translation rules. The order of these rules is the order the
client applies them. Earlier rules win:

| # | Status / condition | Translated to |
|---|--------------------|---------------|
| 1 | 2xx with valid payload | normal return |
| 2 | 429 | `GitHubRateLimitError` with `retry_after: int | None`. Retried up to 3 times within the client before giving up. |
| 3 | 403 with `X-RateLimit-Remaining: 0` (header present and zero) | `GitHubRateLimitError` carrying `reset: int | None`. Retried up to 3 times within the client. |
| 4 | 401 | `GitHubAuthError` |
| 5 | 403 (other than 3 above) | `GitHubAuthError` |
| 6 | 422 | `GitHubValidationError` carrying parsed `errors` from the response body |
| 7 | 5xx | `GitHubTransportError` |
| 8 | Underlying transport raises | `GitHubTransportError` |

The retry/backoff behavior for 429 / 403-with-zero-remaining lives
inside the GitHub client, not the orchestrator. After three attempts,
the client raises `GitHubRateLimitError`. The test
`test_rate_limit_429_is_retried_then_terminates_with_rate_limit_error`
asserts that exactly three POST attempts are issued before the client
gives up. The retry policy between attempts is implementation-defined
(but bounded by the 3-attempt cap) and not part of the public contract.

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

`CodebergNotFoundError` is imported and used in
`test_codeberg_client.py::test_get_issue_404_raises_not_found_with_context`
and is asserted to carry `err.issue_number == 99` and
`err.url.endswith("/issues/99")`.

### 3.5 Repository description behavior

The orchestrator is the single owner of description policy. The
client's `get_repository_description()` returns the literal
`description` field of the GET response, coerced to an empty string
when missing or `null`. **It does not fall back to `"Migrated from
Codeberg"`.** That fallback is the orchestrator's responsibility, and
it is also conditioned on the HTTP call succeeding.

The client's `update_repository_description(description)` issues a
PATCH whenever called. The orchestrator is the single place that
decides when to call it:

- If `repo.description` is non-empty, the orchestrator calls
  `update_repository_description` with that value (after repo
  creation).
- If `repo.description` is empty and the target repo did not exist
  before, the orchestrator fetches the source description; if the
  fetch returns non-empty, the orchestrator passes it to
  `create_repository`; if the fetch returns empty, the orchestrator
  uses `"Migrated from Codeberg"`. No `update_repository_description`
  call is made in either sub-case.
- If `repo.description` is empty and the target repo already existed,
  the orchestrator does not call `update_repository_description` and
  does not fetch the source description.
- On `codeberg.get_repository_description()` HTTP failure (transport
  error, 5xx, etc.), the orchestrator uses `"Migrated from Codeberg"`
  and logs a one-line warning. The fetch error does not become a
  migration failure.

`tests/test_repository_description.py` is rewritten in stage 06 to
drive `MigrationOrchestrator` directly; the four end-state contracts
preserved are:

1. `test_explicit_description_passed_to_create_github_repo` —
   explicit `description` is forwarded to `create_repository`.
2. `test_codeberg_non_empty_description_passed_to_create_github_repo` —
   when no explicit description and source description is non-empty,
   the source description is forwarded to `create_repository`.
3. `test_codeberg_empty_description_falls_back_to_default` — when no
   explicit description and source description is empty, no PATCH is
   issued; `"Migrated from Codeberg"` is the `description` argument to
   `create_repository`.
4. `test_codeberg_metadata_http_error_falls_back_to_default` — when
   the source metadata fetch raises, the orchestrator logs a warning
   and proceeds with `"Migrated from Codeberg"`.
5. `test_codeberg_metadata_connection_error_falls_back_to_default`,
   `test_codeberg_metadata_timeout_falls_back_to_default` — same as 4
   for the specific transport errors.
6. `test_existing_target_does_not_fetch_or_create_repo` — when the
   target repo already exists, the orchestrator does not fetch the
   source description and does not call `create_repository`.
7. `test_existing_target_ignores_explicit_description_argument` —
   when the target repo already exists, an explicit `--description`
   does not cause a `update_repository_description` call.
8. `test_dry_run_does_not_create_repo_or_mutate_description` — under
   `--dry-run`, no HTTP and no state writes.
9. `test_dry_run_does_not_create_repo_when_explicit_description_given`
   — under `--dry-run` with an explicit `--description`, no HTTP and
   no state writes.

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
- **No "I/O at import" module rule.** The "no I/O at module import"
  rule is satisfied by lazy imports of `requests` and other I/O
  libraries; importing the module does not perform network or
  filesystem work, but it is permitted to import I/O libraries for
  later use.

## 5. Collaborator / dependency rules

- `CodebergClient` and `GitHubClient` depend on `Transport`. They do
  not depend on each other.
- `Transport` Protocol depends on nothing.
- `RequestsTransport` depends on `requests` (already a project
  dependency). It imports `requests` lazily inside its methods.
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
  color; the client forwards it. A documented default color is
  applied by the orchestrator when missing. The
  default-color behavior is part of the orchestrator's contract and
  is defined in stage 04. The client itself does not default the
  color.
