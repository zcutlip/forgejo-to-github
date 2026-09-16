"""State persistence for the Forgejo → GitHub migration.

This module owns the on-disk state file (``state.json`` by default in the
caller's working directory). It exposes a path-owning :class:`StateStore`
class with a single, narrow responsibility: load and atomically save the
migration checkpoint.

Design notes
------------

* The state path is **instance-owned**. There is no module-level
  ``STATE_FILE`` constant; the CLI is the only place that derives a path
  (from CLI args or the working directory). Importing this module does
  not surface a canonical path constant.
* The on-disk JSON format is backward-compatible with the legacy
  ``f2gh.save_state`` writer: ``{"source", "target", "repo_created",
  "git_pushed", "migrated": {"<src>": <gh>}}``. Integer issue numbers are
  serialized as JSON object keys (strings) and rehydrated to ``int`` on
  load.
* Writes are atomic: a sibling ``.tmp`` file is written, ``fsync``-ed,
  then ``os.replace``-d onto the destination. A crash mid-``save`` never
  leaves a partially written state file.
* :class:`MigrationState` and :class:`IssueCheckpoint` are the typed
  value objects used by the orchestrator (stage 04). The public
  :meth:`StateStore.load` and :meth:`StateStore.save` API currently
  round-trips the legacy dict shape (``dict[int, int]`` migrated map)
  to preserve backward compatibility with the legacy ``f2gh`` module
  and the tests pinned against it; the typed dataclasses are part of the
  module's public surface for later stages.

This module imports only from the standard library. It performs no
network I/O and spawns no subprocesses.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout

# Top-level keys accepted on the on-disk state file. Any other key
# triggers a ``StateLoadError``. This set is part of the public contract;
# adding a new key is an explicit change to the format.
ACCEPTED_KEYS: frozenset[str] = frozenset(
    {"source", "target", "migrated", "repo_created", "git_pushed", "version", "clone_path"}
)

# Current state-file schema version. Files without a ``"version"`` key
# are accepted (legacy compatibility); files with a present-but-different
# value are rejected. Bumping this is an explicit, breaking change to
# the on-disk format and requires updating the migration story.
_CURRENT_VERSION: int = 1

# Probe written by :func:`_probe_round_trip` to prove the state directory
# accepts and returns bytes before a run is allowed to mutate anything.
_PROBE_NAME = ".f2gh-state-probe"
_PROBE_PAYLOAD = "f2gh state probe\n"


# --- domain value objects ---------------------------------------------------


@dataclass(frozen=True)
class IssueCheckpoint:
    """Per-issue migration checkpoint.

    Frozen dataclass; plain ``@dataclass`` per the binding decisions in
    plan 02's index. ``source_number`` is the Codeberg issue number;
    ``github_number`` is the corresponding GitHub issue number.
    ``state`` carries the Codeberg issue state string (e.g. ``"open"``
    or ``"closed"``); ``closed`` is the boolean mirror used by the
    orchestrator to short-circuit close work on resume.

    The on-disk format omits ``state`` and ``closed`` in this revision
    (plan 02 §3.1); on reload, ``closed`` is reconstructed as ``False``
    and ``state`` as ``"open"`` for all entries. Resume of a partially
    completed issue re-creates all comments and re-issues the close.
    """

    source_number: int
    github_number: int
    state: str
    closed: bool


@dataclass(frozen=True)
class MigrationState:
    """In-memory representation of the migration checkpoint.

    ``migrated`` maps Codeberg issue numbers (int) to
    :class:`IssueCheckpoint` records. The on-disk JSON reduces
    ``migrated`` to ``dict[str, int]`` and discards the
    ``state``/``closed`` fields; see module docstring.
    """

    source: str
    target: str
    repo_created: bool
    git_pushed: bool
    migrated: dict[int, IssueCheckpoint]


# --- exceptions --------------------------------------------------------------


class StateLoadError(Exception):
    """Raised when a state file exists but cannot be loaded safely.

    Carries ``path`` (the file that failed to load) and ``reason`` (a
    redaction-safe human-readable message). The original exception, if
    any, is kept as ``original`` and is **not** included in the string
    form of the error. The on-disk file's contents are never embedded
    in the message: a state file that happens to contain a token must
    not surface that token in error output.
    """

    def __init__(
        self, path: Path, reason: str, original: Exception | None = None
    ) -> None:
        super().__init__(reason)
        self.path: Path = path
        self.reason: str = reason
        self.original: Exception | None = original

    def __str__(self) -> str:  # pragma: no cover - trivial passthrough
        return self.reason


class StateWriteError(Exception):
    """Raised when a state file cannot be written atomically.

    Carries ``path`` and ``reason``; construction mirrors
    :class:`StateLoadError`. The original OSError, if any, is kept as
    ``original`` for logging but is not embedded in the string form.
    """

    def __init__(
        self, path: Path, reason: str, original: Exception | None = None
    ) -> None:
        super().__init__(reason)
        self.path: Path = path
        self.reason: str = reason
        self.original: Exception | None = original

    def __str__(self) -> str:  # pragma: no cover - trivial passthrough
        return self.reason


class StateLockedError(Exception):
    """Raised when the state path is already held by another run.

    A sibling of :class:`StateWriteError` rather than a subclass: lock
    contention is not a write failure, and callers must be able to tell
    "another run holds this path" apart from "this path cannot be written".

    Carries ``state_path``, the resolved path whose lock could not be taken.
    """

    def __init__(self, state_path: Path) -> None:
        super().__init__(f"state path is held by another f2gh run: {state_path}")
        self.state_path: Path = state_path


# --- StateStore --------------------------------------------------------------


class StateStore:
    """Path-owned checkpoint store for a single (source, target) migration.

    The state file path is supplied at construction time and is owned
    by the instance. Two stores with different paths never interfere.

    The on-disk JSON format is backward-compatible with the legacy
    ``f2gh.save_state`` writer; see the module docstring. Writes are
    atomic via :func:`_atomic_write_json`.

    Parameters
    ----------
    state_path:
        Filesystem path of the state file. There is no default; the
        instance owns its location.
    source:
        Codeberg ``owner/repo`` identity this store is bound to. ``load``
        treats a state file whose ``source`` does not match as fresh.
    target:
        GitHub ``owner/repo`` identity this store is bound to. ``load``
        treats a state file whose ``target`` does not match as fresh.
    """

    def __init__(self, state_path: Path, source: str, target: str) -> None:
        self._state_path: Path = Path(state_path)
        self._source: str = source
        self._target: str = target
        self._lock: FileLock | None = None

    # --- properties ----------------------------------------------------------

    @property
    def state_path(self) -> Path:
        """The instance-owned state file path."""
        return self._state_path

    @property
    def source(self) -> str:
        """The Codeberg ``owner/repo`` identity this store is bound to."""
        return self._source

    @property
    def target(self) -> str:
        """The GitHub ``owner/repo`` identity this store is bound to."""
        return self._target

    # --- public API ----------------------------------------------------------

    def load(self) -> dict[str, object]:
        """Load the state file, applying identity and schema validation.

        Returns a plain ``dict`` with the keys ``source``, ``target``,
        ``repo_created``, ``git_pushed``, ``migrated``, and ``clone_path``.
        ``migrated``
        is ``dict[int, int]`` (Codeberg number → GitHub number), the
        same shape the legacy ``f2gh.load_state`` returns. The dataclass
        :class:`MigrationState` is the typed value object used by later
        stages; this method returns the dict form for backward
        compatibility with the tests and the legacy module.

        Returns a fresh default state if:

        * the file does not exist, or
        * the file's ``source`` or ``target`` does not match this
          store's identity (legacy compatibility rule).

        Raises :class:`StateLoadError` for malformed JSON, an
        unsupported ``version``, an unexpected top-level key, or
        unparseable ``migrated`` values. The error message never
        contains the on-disk file contents.
        """
        if not self._state_path.exists():
            return self._fresh_state()

        try:
            with self._state_path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StateLoadError(
                self._state_path,
                "state file contains malformed JSON",
                original=exc,
            ) from exc
        except OSError as exc:
            raise StateLoadError(
                self._state_path,
                f"could not read state file: {exc.strerror or 'OS error'}",
                original=exc,
            ) from exc

        if not isinstance(payload, dict):
            raise StateLoadError(
                self._state_path,
                "state file root must be a JSON object",
            )

        # Schema: reject unknown top-level keys before any further
        # processing, so a typo or a future-format file fails loudly
        # rather than silently being treated as a legacy file.
        unexpected = set(payload.keys()) - ACCEPTED_KEYS
        if unexpected:
            offender = min(unexpected)
            raise StateLoadError(
                self._state_path,
                f"unexpected key in state file: {offender!r}",
            )

        # Identity check: mismatch yields fresh defaults, not an error.
        # This preserves the legacy ``f2gh.load_state`` semantics.
        if (
            payload.get("source") != self._source
            or payload.get("target") != self._target
        ):
            return self._fresh_state()

        # Version handling. Absent version is accepted (legacy files).
        if "version" in payload:
            raw_version = payload["version"]
            if not isinstance(raw_version, int) or isinstance(raw_version, bool):
                raise StateLoadError(
                    self._state_path,
                    "state file 'version' must be an integer",
                )
            if raw_version != _CURRENT_VERSION:
                raise StateLoadError(
                    self._state_path,
                    (
                        f"unsupported state version: {raw_version}; "
                        f"this build only understands version {_CURRENT_VERSION}"
                    ),
                )

        # migrated: must be a dict of string keys → integer values
        # (matching the legacy on-disk shape). Per-comment progress is
        # not persisted; the new format keeps a flat source→github map.
        raw_migrated = payload.get("migrated", {})
        if not isinstance(raw_migrated, dict):
            raise StateLoadError(
                self._state_path,
                "state file 'migrated' must be an object",
            )
        migrated: dict[int, int] = {}
        for key, value in raw_migrated.items():
            if not isinstance(value, int) or isinstance(value, bool):
                raise StateLoadError(
                    self._state_path,
                    "state file 'migrated' values must be integers",
                )
            try:
                int_key = int(key)
            except (TypeError, ValueError) as exc:
                raise StateLoadError(
                    self._state_path,
                    "state file 'migrated' keys must be parseable as integers",
                    original=exc,
                ) from exc
            migrated[int_key] = value

        raw_clone_path = payload.get("clone_path", None)
        if raw_clone_path is not None and not isinstance(raw_clone_path, str):
            raise StateLoadError(
                self._state_path,
                "state file 'clone_path' must be a string",
            )

        return {
            "source": self._source,
            "target": self._target,
            "repo_created": bool(payload.get("repo_created", False)),
            "git_pushed": bool(payload.get("git_pushed", False)),
            "migrated": migrated,
            "clone_path": raw_clone_path,
        }

    def save(
        self,
        repo_created: bool,
        git_pushed: bool,
        migrated: dict[int, int],
        clone_path: str | None = None,
    ) -> None:
        """Persist the migration checkpoint atomically.

        Parameters
        ----------
        repo_created:
            Whether the target GitHub repository was created during
            this migration run.
        git_pushed:
            Whether the Git mirror was pushed successfully.
        migrated:
            Mapping of Codeberg issue numbers to GitHub issue numbers.
        clone_path:
            Location of the cached git mirror checkpointed after a
            successful clone. Omitted from the on-disk payload when
            ``None`` so runs that never cloned keep the pre-existing
            shape.

        Raises
        ------
        StateWriteError
            If the on-disk write fails for any reason (``OSError``,
            ``PermissionError``, etc.). The error message contains the
            reason and the path; it does not contain the payload.
        """
        payload: dict[str, object] = {
            "source": self._source,
            "target": self._target,
            "repo_created": bool(repo_created),
            "git_pushed": bool(git_pushed),
            "migrated": {str(src): gh for src, gh in migrated.items()},
        }
        if clone_path is not None:
            payload["clone_path"] = clone_path
        _atomic_write_json(self._state_path, payload)

    def prepare(self) -> None:
        """Establish the state path for this run before anything is mutated.

        Creates the state file's parent directories, proves the directory
        round-trips bytes (not merely that a file can be created), and takes
        the per-migration run lock. Raises :class:`StateWriteError` when the
        path cannot be created or verified, and :class:`StateLockedError`
        when another run already holds it.
        """
        parent = self._state_path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            _probe_round_trip(parent)
        except OSError as exc:
            reason = exc.strerror or f"OS error preparing state path ({exc.errno})"
            raise StateWriteError(self._state_path, reason, original=exc) from exc

        lock = FileLock(
            self._lock_path,
            timeout=0,
            fallback_to_soft=False,
            preserve_lock_file=True,
        )
        try:
            lock.acquire()
        except Timeout as exc:
            raise StateLockedError(self._state_path) from exc
        self._lock = lock

    def release(self) -> None:
        """Drop the run lock.

        Idempotent, and safe to call without a prior ``prepare``. The lock
        file is deliberately left on disk: unlinking it would race with
        another process acquiring it, and its presence means nothing once
        the lock itself is gone.
        """
        lock = self._lock
        self._lock = None
        if lock is not None:
            try:
                lock.release()
            except OSError:
                pass

    # --- helpers -------------------------------------------------------------

    @property
    def _lock_path(self) -> Path:
        """The run-lock file, beside the state file and per-migration."""
        return self._state_path.with_name(self._state_path.name + ".lock")

    def _fresh_state(self) -> dict[str, object]:
        """Return a fresh default state for this store's identity.

        Used both for the missing-file case and for the identity-mismatch
        case. ``migrated`` is a fresh ``dict`` so callers can mutate
        without aliasing the class default.
        """
        return {
            "source": self._source,
            "target": self._target,
            "repo_created": False,
            "git_pushed": False,
            "migrated": {},
            "clone_path": None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"StateStore(state_path={self._state_path!r}, "
            f"source={self._source!r}, target={self._target!r})"
        )


# --- module-private helpers -------------------------------------------------


def _probe_round_trip(directory: Path) -> None:
    """Write a probe into ``directory``, read it back, and remove it.

    Confirms the directory accepts and returns bytes, which a bare
    existence or creatability check would not. Raises ``OSError`` when the
    bytes do not come back as written.
    """
    probe = directory / _PROBE_NAME
    try:
        probe.write_text(_PROBE_PAYLOAD, encoding="utf-8")
        if probe.read_text(encoding="utf-8") != _PROBE_PAYLOAD:
            raise OSError("state directory did not return the probe bytes")
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def _fsync_directory(directory: Path) -> None:
    """Fsync ``directory`` so a rename into it survives a power loss.

    POSIX only: Windows exposes no way to fsync a directory through ``os``,
    so this is skipped there. Best-effort by design — a write that has
    already landed is not failed over a durability hint.
    """
    if os.name != "posix":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Write ``payload`` to ``path`` atomically.

    The sequence is: open a sibling ``.tmp`` file, ``json.dump`` with
    ``indent=2`` and ``sort_keys=True`` for human readability, append a
    trailing newline, ``fsync`` the temp file, then ``os.replace`` the
    temp file onto the destination. ``os.replace`` is atomic on POSIX
    and on Windows when the destination is on the same filesystem. After
    the rename the parent directory is fsynced so the rename itself is
    durable across a power loss.

    Raises :class:`StateWriteError` on any ``OSError``. The exception
    message is constructed from ``exc.strerror`` (or a generic phrase
    if absent) and does not include the payload, so a payload that
    accidentally contains a token is not surfaced in error output.
    """
    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        # Best-effort cleanup of the orphan temp file. A failure here
        # is not the primary error; surface the original write failure.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        reason = exc.strerror or f"OS error writing state file ({exc.errno})"
        raise StateWriteError(path, reason, original=exc) from exc
