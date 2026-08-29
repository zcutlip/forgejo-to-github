"""Markdown formatting helpers for migrated issues and comments."""

CODEBERG_WEB = "https://codeberg.org"


def format_issue_body(
    source: str, cb_index: int, author: str, date: str, body: str | None
) -> str:
    return (
        f"> **Migrated from Codeberg** "
        f"([Original Issue #{cb_index}]"
        f"({CODEBERG_WEB}/{source}/issues/{cb_index}))\n"
        f"> **Author:** @{author} | **Date:** {date}\n\n"
        f"{body or ''}"
    )


def format_comment_body(author: str, date: str, body: str | None) -> str:
    return f"> **@{author}** commented on {date}:\n\n{body or ''}"
