# RED class: A. Pure unit

"""Pure formatting tests for forgejo_to_github.formatting.

These tests assert deterministic contracts of the public formatting API:

- ``format_issue_body(source, cb_index, author, date, body)``
- ``format_comment_body(author, date, body)``

The functions are pure: inputs are scalars/strings, outputs are strings,
and no I/O is performed. Bodies are passed through verbatim below a fixed
attribution header. There is no API-level toggle to omit the attribution
header; the attribution is always emitted.

Conventions:
- Exact ``==`` assertions on the full returned string.
- No regex, no partial-match assertions.
- Each test exercises one logical property.
"""

from forgejo_to_github.formatting import (
    format_comment_body,
    format_issue_body,
)

# --- format_issue_body: multi-paragraph body preservation ---


def test_format_issue_body_preserves_multiparagraph_body():
    """A body containing multiple paragraphs separated by blank lines is
    emitted verbatim below the attribution header."""
    body_text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    result = format_issue_body(
        source="owner/source",
        cb_index=11,
        author="alice",
        date="2024-01-15",
        body=body_text,
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #11]"
        "(https://codeberg.org/owner/source/issues/11))\n"
        "> **Author:** @alice | **Date:** 2024-01-15\n\n"
        "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    )
    assert result == expected


# --- format_issue_body: Codeberg-relative links preserved verbatim ---


def test_format_issue_body_preserves_codeberg_relative_links():
    """Issue bodies containing Codeberg-style cross-issue references and
    repo-relative links are passed through without rewriting. The body
    substring appears verbatim in the output."""
    body_text = (
        "See [related issue](/owner/source/issues/9) and owner/source#5 for context."
    )
    result = format_issue_body(
        source="owner/source",
        cb_index=12,
        author="alice",
        date="2024-01-16",
        body=body_text,
    )

    assert body_text in result
    # The full string is unchanged aside from the attribution prefix.
    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #12]"
        "(https://codeberg.org/owner/source/issues/12))\n"
        "> **Author:** @alice | **Date:** 2024-01-16\n\n"
        "See [related issue](/owner/source/issues/9) and "
        "owner/source#5 for context."
    )
    assert result == expected


# --- format_issue_body: embedded HTML preserved ---


def test_format_issue_body_preserves_embedded_html():
    """Embedded HTML tags are not escaped or rewritten by the formatter;
    Markdown rendering is left to GitHub."""
    body_text = (
        "<details><summary>Click</summary>\n\n"
        "<pre><code>echo hi</code></pre>\n"
        "</details>"
    )
    result = format_issue_body(
        source="owner/source",
        cb_index=13,
        author="alice",
        date="2024-01-17",
        body=body_text,
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #13]"
        "(https://codeberg.org/owner/source/issues/13))\n"
        "> **Author:** @alice | **Date:** 2024-01-17\n\n"
        "<details><summary>Click</summary>\n\n"
        "<pre><code>echo hi</code></pre>\n"
        "</details>"
    )
    assert result == expected


# --- format_issue_body: empty (non-None) body ---


def test_format_issue_body_with_empty_string_body_is_not_collapsed():
    """An empty-string body yields the attribution header followed by a
    blank line. The function does not raise and does not synthesize
    fallback body content; it does not collapse the issue to an empty
    post because the attribution header is still present."""
    result = format_issue_body(
        source="owner/source",
        cb_index=14,
        author="alice",
        date="2024-01-18",
        body="",
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #14]"
        "(https://codeberg.org/owner/source/issues/14))\n"
        "> **Author:** @alice | **Date:** 2024-01-18\n\n"
        ""
    )
    assert result == expected
    # The output is never the empty string: the attribution header is
    # always emitted.
    assert result != ""


# --- format_issue_body: special-character author names ---


def test_format_issue_body_preserves_author_with_dot():
    """An author name containing a dot (``alice.dev``) is interpolated
    verbatim into the attribution header and does not break Markdown."""
    result = format_issue_body(
        source="owner/source",
        cb_index=15,
        author="alice.dev",
        date="2024-01-19",
        body="Body.",
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #15]"
        "(https://codeberg.org/owner/source/issues/15))\n"
        "> **Author:** @alice.dev | **Date:** 2024-01-19\n\n"
        "Body."
    )
    assert result == expected


def test_format_issue_body_preserves_author_with_hyphen():
    """An author name containing a hyphen (``alice-dev``) is interpolated
    verbatim into the attribution header."""
    result = format_issue_body(
        source="owner/source",
        cb_index=16,
        author="alice-dev",
        date="2024-01-20",
        body="Body.",
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #16]"
        "(https://codeberg.org/owner/source/issues/16))\n"
        "> **Author:** @alice-dev | **Date:** 2024-01-20\n\n"
        "Body."
    )
    assert result == expected


def test_format_issue_body_preserves_unicode_author():
    """An author name containing non-ASCII characters is interpolated
    verbatim into the attribution header without encoding."""
    author = "álice_ñoño"
    result = format_issue_body(
        source="owner/source",
        cb_index=17,
        author=author,
        date="2024-01-21",
        body="Body.",
    )

    expected = (
        "> **Migrated from Codeberg** "
        "([Original Issue #17]"
        "(https://codeberg.org/owner/source/issues/17))\n"
        "> **Author:** @álice_ñoño | **Date:** 2024-01-21\n\n"
        "Body."
    )
    assert result == expected


# --- format_comment_body: multi-paragraph body preservation ---


def test_format_comment_body_preserves_multiparagraph_body():
    """A comment body containing multiple paragraphs separated by blank
    lines is emitted verbatim below the attribution header."""
    body_text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    result = format_comment_body(
        author="bob",
        date="2024-02-02",
        body=body_text,
    )

    expected = (
        "> **@bob** commented on 2024-02-02:\n\n"
        "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
    )
    assert result == expected


# --- format_comment_body: Codeberg-relative links preserved ---


def test_format_comment_body_preserves_codeberg_relative_links():
    """Comment bodies containing Codeberg-style references are passed
    through without rewriting."""
    body_text = (
        "Duplicate of [issue #3](/owner/source/issues/3); see also owner/source#7."
    )
    result = format_comment_body(
        author="bob",
        date="2024-02-03",
        body=body_text,
    )

    assert body_text in result
    expected = (
        "> **@bob** commented on 2024-02-03:\n\n"
        "Duplicate of [issue #3](/owner/source/issues/3); see also "
        "owner/source#7."
    )
    assert result == expected


# --- format_comment_body: embedded HTML preserved ---


def test_format_comment_body_preserves_embedded_html():
    """Embedded HTML tags in a comment body are not escaped or rewritten
    by the formatter."""
    body_text = "<kbd>Ctrl</kbd>+<kbd>C</kbd> to copy."
    result = format_comment_body(
        author="bob",
        date="2024-02-04",
        body=body_text,
    )

    expected = (
        "> **@bob** commented on 2024-02-04:\n\n<kbd>Ctrl</kbd>+<kbd>C</kbd> to copy."
    )
    assert result == expected


# --- format_comment_body: empty (non-None) body ---


def test_format_comment_body_with_empty_string_body_is_not_silently_dropped():
    """An empty-string body yields the attribution header followed by a
    blank line. The function does not raise and does not silently drop
    the comment, because the attribution header is always emitted.

    Note: the current implementation does NOT synthesize a fallback
    paragraph; the output body is empty. This documents actual behavior
    and matches what ``body=None`` already produces. See spec §6.3 for
    the desired "single non-empty paragraph" contract, which is a gap
    between spec and implementation."""
    result = format_comment_body(
        author="eve",
        date="2024-05-05",
        body="",
    )

    expected = "> **@eve** commented on 2024-05-05:\n\n"
    assert result == expected
    # The output is never the empty string: the attribution header is
    # always emitted, so the comment is not silently dropped.
    assert result != ""


# --- format_comment_body: special-character author names ---


def test_format_comment_body_preserves_author_with_dot():
    """An author name containing a dot is interpolated verbatim into the
    comment attribution header."""
    result = format_comment_body(
        author="carol.dev",
        date="2024-03-03",
        body="Comment body.",
    )

    expected = "> **@carol.dev** commented on 2024-03-03:\n\nComment body."
    assert result == expected


def test_format_comment_body_preserves_author_with_hyphen():
    """An author name containing a hyphen is interpolated verbatim into
    the comment attribution header."""
    result = format_comment_body(
        author="carol-dev",
        date="2024-03-04",
        body="Comment body.",
    )

    expected = "> **@carol-dev** commented on 2024-03-04:\n\nComment body."
    assert result == expected


def test_format_comment_body_preserves_unicode_author():
    """An author name containing non-ASCII characters is interpolated
    verbatim into the comment attribution header without encoding."""
    author = "cârol_ñoño"
    result = format_comment_body(
        author=author,
        date="2024-03-05",
        body="Comment body.",
    )

    expected = "> **@cârol_ñoño** commented on 2024-03-05:\n\nComment body."
    assert result == expected
