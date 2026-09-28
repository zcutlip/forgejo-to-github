# Plan 14 — `write_version` must preserve the version file

**Status:** spec — revised after audit (`plans/14-write-version-preserves-file-audit-2026-09-28.md`), awaiting review.

**Contract change to:** `submodules/repo-mgmt-scripts/src/issue-branch`
**References:** none filed. `repo-mgmt-scripts` is a shared tool with no
per-consumer issue tracker. Discovered while preparing the `forgejo-to-github`
1.4.0 release.

**Branch:** `main`, by explicit user direction. This deviates from the
one-branch-per-plan convention in `AGENTS.md §4`; the user owns both
repositories and directed the work onto `main` directly.

---

## 1. Problem

`write_version` truncates the version file and writes a single line.

```sh
write_version() {
    ib_file="$(about_file)"
    if [ ! -f "$ib_file" ]; then
        quit "No '$ib_file' found to write version" 1
    fi
    printf '__version__ = "%s"\n' "$1" > "$ib_file"
    unset ib_file
}
```

It assumes `__about__.py` contains nothing but a version assignment. Any target
whose version file also carries a docstring, `__title__`, `__summary__`, or
`__all__` loses all of it on the first write.

**Observed damage.** In `forgejo-to-github`, the `release 1.4.0` commit
(`3ca0c57`, produced by the `finish` flow) reduced a 12-line module to

```python
__version__ = "1.4.0"
```

`forgejo_to_github/about.py:8` imports `__summary__` and `__title__` from that
module, so the package stopped importing. The suite went red with 2 failures and
4 collection errors, and the release could not be cut. Restoring the file by
hand fixed it.

**Call sites, all affected:** `create` (:327), `resume` (:367), `bump-version`
(:460), `finish` recovery (:496), `finish` (:528).

## 2. Why the tests did not catch it

`make_project` writes a bare fixture:

```sh
printf '__version__ = "%s"\n' "$_version" > "$_dir/sample/__about__.py"
```

There is nothing in it to destroy, so the destructive write is invisible. The
bare fixture encodes a simpler shape than reality, and every assertion built on
it is blind to the difference.

## 3. Design

Rewrite **only** the `__version__` assignment; leave every other byte alone.

The repository already parses this file the right way. `read_about_version.py`
AST-parses `__about__.py` to read `__version__` without importing the package
or its dependencies, and `functions.sh:103` invokes it. The write side gets the
mirror-image sibling: parse, locate the assignment node, splice in the new
value, write the file back.

**Splice mechanism, pinned.** The replacement is a **text-level** substitution:
locate the value node's source span via `ast.get_source_segment` (or the node's
`lineno`/`col_offset`/`end_lineno`/`end_col_offset`) and replace only that span.
Whole-tree `ast.unparse` is explicitly **rejected** — it discards comments and
normalises quoting and spacing, which would violate the byte-preservation
requirement outright. All other bytes in the file are copied through verbatim.

A `sed` substitution on the assignment line is the lighter alternative and is
rejected: it cannot distinguish the plain form from the annotated form
(`__version__: str = "..."`) without extra patterns, and it would also match a
version-looking string appearing elsewhere in the file.

**Replacement quoting.** The value span is replaced with a double-quoted literal
(`"1.4.0"`), not `repr()`'s single-quoted form. Existing files use double quotes,
so a double-quoted splice leaves the write invisible in diffs.

**Behaviour decisions, each needing to be pinned in the spec:**

- **Missing `__version__` assignment.** Fail loudly rather than append one.
  Today truncation would silently satisfy the write; preserving content means
  an absent assignment is a real error and must `quit`.
- **Annotated form.** `__version__: str = "1.0.0"` is rewritten in place,
  preserving the annotation. `read_about_version.py` already treats both forms
  alike, so the write side must too.
- **Encoding.** Read and write UTF-8, matching `read_about_version.py`.
- **Trailing newline.** Preserve the file's existing final-newline state rather
  than normalising it.

## 4. Implementation

### `src/write_about_version.py` (new)

Sibling of `read_about_version.py`, same CLI shape and stdlib-only constraint:

```
write_about_version.py <path> <new-version>
```

Parses with `ast`, finds the first string assignment to `__version__` (plain or
annotated), splices the new value at that node's location, rewrites the file.
Exits nonzero with a message on: unparseable file, no `__version__` assignment,
or a write failure. Must not import the target package.

### `src/issue-branch`

`write_version` becomes a call to the new script, keeping its existing missing-
file `quit`:

- resolve `about_file()` as today
- invoke `python3 "$DIRNAME/write_about_version.py" "$ib_file" "$1"`, `quit` on
  nonzero status with the script's message

### `install`

Add `write_about_version.py` to the file list at `install:31`, alongside
`read_about_version.py`.

### Naming and mode

`write_about_version.py` follows `read_about_version.py` exactly: a `.py` helper
in `src/`, mode `755` to match its sibling's existing mode (not a
non-executable mode — that was an error in an earlier revision of this plan).

The `script-must-have-extension` / `script-must-not-have-extension` hooks do
**not** govern this: both are scoped to `types: [shell, ...]`
(`.pre-commit-config.yaml:13-18`), so a `.py` file is outside their reach
entirely. The basis is the sibling precedent, nothing else.

### Main repo consumption

`forgejo-to-github` consumes the submodule through `scripts/` symlinks, one of
which already exists: `scripts/read_about_version.py ->
../submodules/repo-mgmt-scripts/src/read_about_version.py`. `write_version`
invokes `python3 "$DIRNAME/write_about_version.py"` where `$DIRNAME` resolves to
`scripts/`, so **`scripts/write_about_version.py` must exist as a matching
symlink** before the submodule bump lands, or the main repo's `issue-branch`
breaks on its first write.

**User-owned, with the submodule pointer bump.** Both are listed in §9 so they
are not lost.

## 5. Tests — `tests/test_issue_branch`

New fixture variant, `rich_project`, whose `sample/__about__.py` carries a
docstring, `__title__`, `__version__`, `__summary__`, and `__all__`. Follows
the existing `make_project` shape and takes the same optional-version argument.

**The fixture uses the plain form** (`__version__ = "1.3.0"`), not the annotated
form. The shared helpers `assert_version` (`:109`), `current_version_of`
(`:166`), and the `git show` assertion (`:324`) all match with
`sed -n 's/^__version__ = "\(.*\)"/\1/p'` — plain form only. An annotated
`rich_project` would make every preservation assertion read as empty and fail
for a helper limitation rather than a contract violation. The annotated form has
its own dedicated bullet below.

**Fixture copy list.** `make_project` copies exactly three files into
`scripts/` (`:220-222`): `issue-branch`, `functions.sh`, and
`read_about_version.py`. Both `make_project` and `rich_project` must also copy
**`write_about_version.py`** into `scripts/`, or `write_version` fails on a
missing file and every test that runs `create` fails for a reason
indistinguishable from a broken implementation.

- **Preservation across each mutating subcommand.** `create`, `resume`,
  `bump-version`, and `finish` each leave the docstring, `__title__`,
  `__summary__`, and `__all__` intact, and each updates `__version__` to the
  expected value. All four call `write_version` (`:327`, `:367`, `:460`, `:496`
  or `:528`).
- **Annotated form.** A fixture using `__version__: str = "1.3.0"` is rewritten
  in place — annotation preserved, no duplicate assignment appended.
- **Trailing newline.** A fixture whose file lacks a final newline keeps it
  that way after a write.
- **Unit-level coverage of the new script.** `write_about_version.py` directly:
  preserves content, rewrites both forms, rejects a missing assignment,
  rejects an unparseable file, preserves comments and quoting (the
  `ast.unparse` regression guard).

**Missing-assignment coverage is unit-level only.** A subcommand-level test for
it cannot fail for the contract reason: every mutating subcommand reads the
version first via `current_version` → `read_about_version.py`, which already
exits 1 on a missing assignment (`:49-50`). Such a test would abort at the read
stage before `write_version` is reached, and would **pass** against the current
destructive code — truncation succeeds and exits 0. The write-path contract is
pinneable only by invoking the script directly.

Each assertion must be shown to fail against the current destructive
`write_version` before implementation — the same discipline the repo's RED
honesty rule requires. The preservation assertions fail immediately against
truncation; the annotated, trailing-newline, and quoting assertions are the ones
to verify individually rather than assume.

## 6. Documentation

- `submodules/repo-mgmt-scripts/AGENTS.md` — the Config Resolution section
  already documents version *reading* via `read_about_version.py`. Add the
  writing counterpart alongside it.
- `src/example_project_settings.sh` — comments there describe version
  resolution; add a note that writes preserve the file.
- No README change (it documents no flags or config internals).

## 7. Breaking change

None. This narrows a destructive write to a surgical one. Targets whose
`__about__.py` held only a version get byte-identical results; targets with
richer files stop losing content.

## 8. Verification

1. `tests/test_issue_branch` — the full harness.
2. `pre-commit run --all-files` — shellcheck, extension policy, YAML/JSON.
3. An end-to-end run against a throwaway target project that has a rich
   `__about__.py`: `create` → `bump-version` → `finish`, confirming the file
   survives all three and the version updates at each step. The throwaway
   project is populated by **running `./install <dir>`** from the submodule,
   which is the documented consumption path and exercises the `install` file
   list added in §4 — not a manual `cp` of the scripts.
4. Confirm the `forgejo-to-github` shape specifically: a copy of the real
   12-line `__about__.py` survives a `create`/`finish` cycle with its metadata
   intact.

## 9. Delivery steps — both user-owned

These are outside the implementation but must not be lost, or the fix does not
reach the consumer:

- **`scripts/write_about_version.py` symlink** in `forgejo-to-github`, mirroring
  the existing `scripts/read_about_version.py` link. Without it the main repo's
  `issue-branch` breaks on its first write.
- **Submodule pointer bump** in `forgejo-to-github`, from the current `e035871`
  to the commit carrying this fix.

## 10. Out of scope

- The `forgejo-to-github` `__about__.py` restoration, done already as a
  separate manual fix.
- Any change to `read_about_version.py` beyond adding its sibling.
- The `resume` re-level behaviour and the `release` version ownership, both
  settled in `plans/12-release-version-ownership.md`.
