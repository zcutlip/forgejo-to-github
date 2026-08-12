#!/usr/bin/env python3

import argparse
import json
import os
import random
import subprocess
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


def load_state(source: str, target: str) -> dict[int, int]:
    """Load state.json if it matches source/target, otherwise start fresh."""
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        state = json.load(f)
    if state.get("source") != source or state.get("target") != target:
        return {}
    return {int(k): v for k, v in state.get("migrated", {}).items()}


def save_state(source: str, target: str, migrated: dict[int, int]) -> None:
    """Atomically write state."""
    state = {"source": source, "target": target, "migrated": migrated}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate issues from Codeberg/Forgejo to GitHub."
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
        help="Preview migration without creating GitHub issues",
    )
    return parser.parse_args()


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
        res = requests.get(url, headers=cb_headers(), params=params)
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
    res = requests.get(url, headers=cb_headers())
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


def migrate(source: str, target: str, dry_run: bool) -> None:
    """Main migration logic."""
    migrated = load_state(source, target)
    stats = {"created": 0, "skipped": 0, "comments": 0, "failures": 0}

    print("Fetching issues from Codeberg...")
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
        print(f"Found {len(cb_issues)} issues to migrate.\n")

    for issue in new_issues:
        cb_index: int = issue["number"]
        original_author: str = issue["user"]["username"]
        created_at: str = issue["created_at"].split("T")[0]
        title: str = issue["title"]
        state: str = issue["state"]
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
            if state == "closed":
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

            if state == "closed":
                close_github_issue(target, gh_number)
                time.sleep(0.3)

            migrated[cb_index] = gh_number
            save_state(source, target, migrated)
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

    # Summary
    print("Migration complete!")
    print(f"  Issues created: {stats['created']}")
    if stats["skipped"]:
        print(f"  Issues skipped: {stats['skipped']} (already migrated)")
    print(f"  Comments posted: {stats['comments']}")
    if stats["failures"]:
        print(f"  Failures: {stats['failures']}")


def main() -> None:
    args = parse_args()
    migrate(args.source, args.target, args.dry_run)


if __name__ == "__main__":
    main()
