#!/usr/bin/env python3

import argparse
import json
import os
import random
import shutil
import subprocess
import tempfile
import time

import requests

CODEBERG_BASE = "https://codeberg.org/api/v1"
GITHUB_BASE = "https://api.github.com"

STATE_FILE = "state.json"


def get_github_token() -> str:
    """Get GitHub token from env or gh CLI."""
    token = os.getenv("GITHUB_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise SystemExit("GITHUB_TOKEN not set and 'gh auth token' failed.")


def codeberg_token() -> str:
    token = os.getenv("CODEBERG_TOKEN")
    if not token:
        raise SystemExit("CODEBERG_TOKEN not set.")
    return token


def cb_headers() -> dict[str, str]:
    return {"Authorization": f"token {codeberg_token()}", "Accept": "application/json"}


def gh_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_github_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def gh_request(
    method: str, url: str, max_retries: int = 3, **kwargs
) -> requests.Response:
    """GitHub API request with rate-limit backoff."""
    for attempt in range(max_retries):
        resp = requests.request(method, url, headers=gh_headers(), **kwargs)
        if resp.status_code in (403, 429):
            retry_after = int(resp.headers.get("Retry-After", 0))
            if retry_after == 0:
                reset_epoch = int(resp.headers.get("X-RateLimit-Reset", 0))
                retry_after = (
                    max(reset_epoch - int(time.time()), 1) if reset_epoch else 60
                )
            delay = retry_after + random.uniform(0, 2)
            if attempt < max_retries - 1:
                print(f"  Rate limited. Waiting {delay:.0f}s...")
                time.sleep(delay)
                continue
        resp.raise_for_status()
        remaining = int(resp.headers.get("X-RateLimit-Remaining", 100))
        if remaining < 10:
            time.sleep(2)
        return resp
    raise RuntimeError(f"GitHub request failed after {max_retries} retries")


def load_state(source: str, target: str) -> dict[str, bool | dict[int, int]]:
    """Load state.json if it matches source/target, otherwise start fresh."""
    if not os.path.exists(STATE_FILE):
        return {"repo_created": False, "git_pushed": False, "migrated": {}}
    with open(STATE_FILE) as f:
        state = json.load(f)
    if state.get("source") != source or state.get("target") != target:
        return {"repo_created": False, "git_pushed": False, "migrated": {}}
    return {
        "repo_created": bool(state.get("repo_created", False)),
        "git_pushed": bool(state.get("git_pushed", False)),
        "migrated": {int(k): v for k, v in state.get("migrated", {}).items()},
    }


def save_state(
    source: str,
    target: str,
    repo_created: bool,
    git_pushed: bool,
    migrated: dict[int, int],
) -> None:
    """Atomically write state."""
    state: dict[str, object] = {
        "source": source,
        "target": target,
        "repo_created": repo_created,
        "git_pushed": git_pushed,
        "migrated": migrated,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate repos from Codeberg/Forgejo to GitHub."
    )
    parser.add_argument(
        "--source",
        required=True,
        metavar="OWNER/REPO",
        help="Source repo on Codeberg (e.g. 'myuser/myproject')",
    )
    parser.add_argument(
        "--target",
        required=True,
        metavar="OWNER/REPO",
        help="Target repo on GitHub (e.g. 'myuser/myproject')",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview migration without making any changes",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip all interactive prompts (for scripting/CI)",
    )
    parser.add_argument(
        "--skip-git",
        action="store_true",
        help="Skip git mirror clone/push (only migrate issues)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Create target repo as public (default: private)",
    )
    parser.add_argument(
        "--description",
        metavar="TEXT",
        default="Migrated from Codeberg",
        help='Repo description on GitHub (default: "Migrated from Codeberg")',
    )
    return parser.parse_args()


def confirm(prompt: str, *, yes: bool) -> bool:
    """Ask user to confirm an action. Skips prompt if --yes flag is set."""
    if yes:
        return True
    while True:
        answer = input(f"{prompt} [y/N] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("", "n", "no"):
            return False
        print('Please answer "y" or "n".')


def check_target_repo(target: str) -> dict[str, object] | None:
    """Check if target repo exists on GitHub. Returns repo info or None."""
    url = f"{GITHUB_BASE}/repos/{target}"
    resp = requests.get(url, headers=gh_headers(), timeout=30)
    if resp.status_code == 200:
        return resp.json()
    if resp.status_code == 404:
        return None
    if resp.status_code == 403:
        raise SystemExit(f"Token lacks access to '{target}' (HTTP 403).")
    resp.raise_for_status()
    return None


def create_github_repo(
    target: str, description: str, public: bool
) -> dict[str, object]:
    """Create a new repo on GitHub. Tries personal then org."""
    owner, repo = target.split("/", 1)
    payload = {
        "name": repo,
        "private": not public,
        "description": description,
        "has_issues": True,
    }

    # Try personal repo first
    url = f"{GITHUB_BASE}/user/repos"
    resp = requests.post(url, headers=gh_headers(), json=payload)
    if resp.status_code == 201:
        return resp.json()

    # Fallback: try org repo
    url = f"{GITHUB_BASE}/orgs/{owner}/repos"
    resp = gh_request("POST", url, json=payload)
    return resp.json()


def mirror_git_repo(source: str, target: str, dry_run: bool) -> None:
    """Clone mirror from Codeberg and push branches+tags to GitHub."""
    token = get_github_token()
    source_url = f"https://codeberg.org/{source}.git"
    target_url = f"https://x-access-token:{token}@github.com/{target}.git"

    repo_name = target.split("/", 1)[1]
    tmpdir = tempfile.mkdtemp(prefix=f"f2gh-{repo_name}-")

    try:
        print(f"Cloning mirror from {source_url}...")
        if not dry_run:
            subprocess.run(
                ["git", "clone", "--mirror", source_url, tmpdir],
                check=True,
                capture_output=True,
                text=True,
            )

        print("Pushing branches and tags to GitHub...")
        if not dry_run:
            subprocess.run(
                ["git", "-C", tmpdir, "push", target_url, "--all"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", tmpdir, "push", target_url, "--tags"],
                check=True,
                capture_output=True,
                text=True,
            )

        print("  Git push complete.")
    finally:
        if not dry_run:
            shutil.rmtree(tmpdir, ignore_errors=True)


def fetch_all_codeberg_issues(source: str) -> list[dict]:
    """Fetch all issues (excluding PRs) from Codeberg."""
    issues: list[dict] = []
    page = 1
    while True:
        url = f"{CODEBERG_BASE}/repos/{source}/issues"
        params = {
            "state": "all",
            "type": "issues",
            "sort": "asc",
            "page": page,
            "limit": 50,
        }
        res = requests.get(url, headers=cb_headers(), params=params, timeout=30)
        res.raise_for_status()
        data: list[dict] = res.json()
        if not data:
            break
        issues.extend(data)
        page += 1
    return issues


def fetch_codeberg_comments(source: str, issue_index: int) -> list[dict]:
    """Fetch comments for a specific issue index on Codeberg."""
    url = f"{CODEBERG_BASE}/repos/{source}/issues/{issue_index}/comments"
    res = requests.get(url, headers=cb_headers(), timeout=30)
    res.raise_for_status()
    return res.json()


def create_github_issue(target: str, title: str, body: str, labels: list[str]) -> dict:
    """POST a new issue to GitHub."""
    url = f"{GITHUB_BASE}/repos/{target}/issues"
    payload = {"title": title, "body": body, "labels": labels}
    resp = gh_request("POST", url, json=payload)
    return resp.json()


def create_github_comment(target: str, issue_number: int, body: str) -> None:
    """POST a comment to an existing GitHub issue."""
    url = f"{GITHUB_BASE}/repos/{target}/issues/{issue_number}/comments"
    gh_request("POST", url, json={"body": body})


def close_github_issue(target: str, issue_number: int) -> None:
    """PATCH a GitHub issue to set its state to closed."""
    url = f"{GITHUB_BASE}/repos/{target}/issues/{issue_number}"
    gh_request("PATCH", url, json={"state": "closed"})


def format_issue_body(cb_index: int, author: str, date: str, body: str | None) -> str:
    return (
        f"> 📦 **Migrated from Codeberg** (Original Issue #{cb_index})\n"
        f"> **Author:** @{author} | **Date:** {date}\n\n"
        f"{body or ''}"
    )


def format_comment_body(author: str, date: str, body: str | None) -> str:
    return f"> **@{author}** commented on {date}:\n\n{body or ''}"


def migrate(
    source: str,
    target: str,
    dry_run: bool,
    yes: bool,
    skip_git: bool,
    public: bool,
    description: str,
) -> None:
    """Main migration logic."""
    state = load_state(source, target)
    migrated: dict[int, int] = state["migrated"]  # type: ignore[assignment]
    repo_created: bool = state["repo_created"]  # type: ignore[assignment]
    git_pushed: bool = state["git_pushed"]  # type: ignore[assignment]

    stats: dict[str, int] = {
        "created": 0,
        "skipped": 0,
        "comments": 0,
        "failures": 0,
    }

    # --- Phase 1: Pre-flight & repo setup ---
    print(f"Checking target repo '{target}' on GitHub...")
    repo_info = check_target_repo(target)

    if repo_info is None:
        # Repo doesn't exist — prompt to create
        if not confirm(f"Target '{target}' does not exist. Create it?", yes=yes):
            raise SystemExit("Aborted.")

        visibility = "public" if public else "private"
        print(f"Creating {visibility} repo '{target}'...")
        if not dry_run:
            create_github_repo(target, description, public)
            repo_created = True
            save_state(source, target, repo_created, git_pushed, migrated)
        else:
            print(f"  [DRY RUN] Would create {visibility} repo '{target}'")
            repo_created = True
    else:
        # Repo exists — warn if it has issues
        open_count = repo_info.get("open_issues_count", 0)
        if (
            open_count > 0
            and not yes
            and not confirm(
                f"WARNING: Target repo '{target}' already has {open_count} "
                f"issues. Migration will add new issues alongside them. "
                f"Continue?",
                yes=False,  # always prompt: overriding existing issues is dangerous
            )
        ):
            raise SystemExit("Aborted.")
        if open_count > 0 and yes:
            print(f"  Target repo '{target}' has {open_count} existing issues.")
        print(f"  Target repo '{target}' exists and is accessible.")

    # --- Phase 2: Git mirror ---
    if not skip_git:
        if not git_pushed:
            if not dry_run:
                mirror_git_repo(source, target, dry_run=False)
                git_pushed = True
                save_state(source, target, repo_created, git_pushed, migrated)
            else:
                print("[DRY RUN] Would clone mirror from Codeberg and push to GitHub")
        else:
            print("  Git already pushed (from previous run). Skipping.")
    else:
        print("  Skipping git mirror (--skip-git).")

    # --- Phase 3: Issue migration ---
    print("\nFetching issues from Codeberg...")
    cb_issues = fetch_all_codeberg_issues(source)

    new_issues = [i for i in cb_issues if i["number"] not in migrated]
    if len(new_issues) < len(cb_issues):
        stats["skipped"] = len(cb_issues) - len(new_issues)
        print(
            f"Found {len(cb_issues)} total issues. "
            f"{stats['skipped']} already migrated. "
            f"{len(new_issues)} to process.\n",
        )
    else:
        print(f"Found {len(new_issues)} issues to migrate.\n")

    for issue in new_issues:
        cb_index: int = issue["number"]
        original_author: str = issue["user"]["username"]
        created_at: str = issue["created_at"].split("T")[0]
        title: str = issue["title"]
        state_str: str = issue["state"]
        labels = [label["name"] for label in issue.get("labels", [])]

        formatted_body = format_issue_body(
            cb_index, original_author, created_at, issue.get("body")
        )

        if dry_run:
            print(f"[DRY RUN] Would create issue: '{title}'")
            print(f"  Labels: {labels}")
            print(f"  Body: {formatted_body[:120]}...")
            comments = fetch_codeberg_comments(source, cb_index)
            for comment in comments:
                if comment.get("type") != "Comment":
                    continue
                print(
                    f"  [DRY RUN] Would post comment "
                    f"from @{comment['user']['username']}"
                )
            if state_str == "closed":
                print("  [DRY RUN] Would close issue")
            print()
            stats["created"] += 1
            continue

        print(f"Migrating Issue #{cb_index}: '{title}'...")
        try:
            gh_issue = create_github_issue(target, title, formatted_body, labels)
            gh_number: int = gh_issue["number"]
            time.sleep(0.3)

            comments = fetch_codeberg_comments(source, cb_index)
            for comment in comments:
                if comment.get("type") != "Comment":
                    continue
                formatted_comment = format_comment_body(
                    comment["user"]["username"],
                    comment["created_at"].split("T")[0],
                    comment.get("body"),
                )
                create_github_comment(target, gh_number, formatted_comment)
                stats["comments"] += 1
                time.sleep(0.3)

            if state_str == "closed":
                close_github_issue(target, gh_number)
                time.sleep(0.3)

            migrated[cb_index] = gh_number
            save_state(source, target, repo_created, git_pushed, migrated)
            stats["created"] += 1
            print(f"  Successfully created GitHub Issue #{gh_number}\n")
        except (
            requests.HTTPError,
            requests.ConnectionError,
            requests.Timeout,
            KeyError,
        ) as e:
            stats["failures"] += 1
            print(f"  FAILED: {e}\n")

    print(
        "\nMigration complete!"
        f"\n  Target repo: {target}"
        f"\n  Repo: {'created' if repo_created else 'existing'}"
        f"\n  Git: {'pushed' if git_pushed else 'skipped'}"
        f"\n  Issues created: {stats['created']}"
    )
    if stats["skipped"]:
        print(f"  Issues skipped: {stats['skipped']} (already migrated)")
    print(f"  Comments posted: {stats['comments']}")
    if stats["failures"]:
        print(f"  Failures: {stats['failures']}")


def main() -> None:
    args = parse_args()
    migrate(
        source=args.source,
        target=args.target,
        dry_run=args.dry_run,
        yes=args.yes,
        skip_git=args.skip_git,
        public=args.public,
        description=args.description,
    )


if __name__ == "__main__":
    main()
