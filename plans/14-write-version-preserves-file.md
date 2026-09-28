# Plan 14 — `write_version` must preserve the version file

**Status:** spec — awaiting review.

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

There is nothing in it to destroy, so the destructive write is invisible. This
is the same fixture-blindness class as Finding 1 of
`plans/archive/13-test-audit-2026-09-26.md`: the shared fixture encodes a
simpler shape than reality, and the assertion cannot see the difference.

## 3. Design

Rewrite **only** the `__version__` assignment; leave every other byte alone.

The repository already parses this file the right way. `read_about_version.py`
AST-parses `__about__.py` to read `__version__` without importing the package
or its dependencies, and `functions.sh:103` invokes it. The write side gets the
mirror-image sibling: parse, locate the assignment node, splice in the new
value, write the file back.

A `sed` substitution on the assignment line is the lighter alternative and is
rejected: it cannot distinguish the plain form from the annotated form
(`__version__: str = "..."`) without extra patterns, and it would also match a
version-looking string appearing elsewhere in the file.

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
`read_about_version.py`, and set the executable bit per the naming policy
(non-executable helper keeps `.py`, matching its sibling).

### Naming policy

`write_about_version.py` follows `read_about_version.py`: a non-executable
helper with a `.py` extension. The `script-must-have-extension` /
`script-must-not-have-extension` hooks govern this; confirm against
`.pre-commit-config.yaml` rather than assuming.

## 5. Tests — `tests/test_issue_branch`

New fixture variant, `rich_project`, whose `sample/__about__.py` carries a
docstring, `__title__`, `__version__`, `__summary__`, and `__all__`. Follows
the existing `make_project` shape and takes the same optional-version argument.

- **Preservation across each mutating subcommand.** `create`, `bump-version`,
  and `finish` each leave the docstring, `__title__`, `__summary__`, and
  `__all__` intact, and each updates `__version__` to the expected value.
- **Annotated form.** A fixture using `__version__: str = "1.3.0"` is rewritten
  in place — annotation preserved, no duplicate assignment appended.
- **Missing assignment.** A `__about__.py` with no `__version__` assignment
  fails loudly, non-zero, and the file is left unchanged.
- **Trailing newline.** A fixture whose file lacks a final newline keeps it
  that way after a write.
- **Unit-level coverage of the new script.** `write_about_version.py` directly:
  preserves content, rewrites both forms, rejects a missing assignment,
  rejects an unparseable file.

Each assertion must be shown to fail against the current destructive
`write_version` before implementation — the same discipline the repo's RED
honesty rule requires. The preservation assertions will fail immediately
against truncation; the annotated and trailing-newline assertions are the ones
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
   survives all three and the version updates at each step.
4. Confirm the `forgejo-to-github` shape specifically: a copy of the real
   12-line `__about__.py` survives a `create`/`finish` cycle with its metadata
   intact.

## 9. Out of scope

- The `forgejo-to-github` `__about__.py` restoration, done already as a
  separate manual fix.
- Any change to `read_about_version.py` beyond adding its sibling.
- The `resume` re-level behaviour and the `release` version ownership, both
  settled in `plans/12-release-version-ownership.md`.
