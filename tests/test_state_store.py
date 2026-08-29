"""RED-stage tests for the StateStore domain object.

These tests define the intended contract for forgejo_to_github.state.StateStore
as approved during plan 02 (package refactor). They are expected to fail RED
until the StateStore implementation lands; the parent agent will run the
focused RED test and surface the result before any implementation work begins.

Contract points under test:
1. StateStore(path, source, target).load() returns a typed/default state when
   the file is absent.
2. StateStore ignores a checkpoint whose source or target identity does not
   match, returning fresh defaults.
3. StateStore.save() persists source/target/repo_created/git_pushed/migrated
   using atomic replacement (os.replace). Integer migrated keys are serialized
   as JSON strings and restored as ints.
4. StateStore does not expose or require a module-level STATE_FILE; its path
   is owned by the instance.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from forgejo_to_github.state import StateStore

# --- helpers ----------------------------------------------------------------


def _store(
    tmp_path, source: str = "owner/source", target: str = "owner/target"
) -> StateStore:
    """Build a StateStore rooted at a per-test tmp_path/state.json."""
    return StateStore(tmp_path / "state.json", source, target)


# --- contract 1: load() with no file returns typed/default state --------------


def test_load_returns_default_state_when_file_absent(tmp_path):
    store = _store(tmp_path)

    state = store.load()

    assert state == {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": False,
        "git_pushed": False,
        "migrated": {},
    }


# --- contract 2: identity mismatch is ignored --------------------------------


def test_load_ignores_checkpoint_with_mismatched_source(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "source": "someone-else/source",
                "target": "owner/target",
                "repo_created": True,
                "git_pushed": True,
                "migrated": {"1": 99},
            }
        )
    )
    store = _store(tmp_path)

    state = store.load()

    # Mismatched checkpoint must be discarded; defaults returned.
    assert state == {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": False,
        "git_pushed": False,
        "migrated": {},
    }


def test_load_ignores_checkpoint_with_mismatched_target(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(
        json.dumps(
            {
                "source": "owner/source",
                "target": "someone-else/target",
                "repo_created": True,
                "git_pushed": True,
                "migrated": {"1": 99},
            }
        )
    )
    store = _store(tmp_path)

    state = store.load()

    assert state == {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": False,
        "git_pushed": False,
        "migrated": {},
    }


# --- contract 3: save() persists fields + uses os.replace --------------------


def test_save_persists_all_fields_and_uses_atomic_replace(tmp_path):
    store = _store(tmp_path)

    store.save(
        repo_created=True,
        git_pushed=False,
        migrated={12: 34, 7: 8},
    )

    # JSON on disk: integer migrated keys must be serialized as strings.
    on_disk = json.loads((tmp_path / "state.json").read_text())
    assert on_disk == {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": True,
        "git_pushed": False,
        "migrated": {"12": 34, "7": 8},
    }


def test_save_calls_os_replace_for_atomic_write(tmp_path):
    store = _store(tmp_path)

    with patch("os.replace") as replace_spy:
        store.save(repo_created=False, git_pushed=False, migrated={})

    # Atomic replacement is the contract; one call, swapping the temp file
    # over the final state path.
    assert replace_spy.call_count == 1
    # The destination of os.replace must be the instance-owned state path.
    args, _kwargs = replace_spy.call_args
    assert args[1] == tmp_path / "state.json"


def test_round_trip_restores_int_keys(tmp_path):
    """Keys serialized as JSON strings must come back as ints on load()."""
    store = _store(tmp_path)
    store.save(repo_created=True, git_pushed=True, migrated={3: 30, 9: 90})

    reloaded = store.load()

    assert reloaded["repo_created"] is True
    assert reloaded["git_pushed"] is True
    assert reloaded["migrated"] == {3: 30, 9: 90}
    # Be explicit: keys are ints, not strings, after the round trip.
    for key in reloaded["migrated"]:
        assert isinstance(key, int), f"key {key!r} should be int, got {type(key)}"


# --- contract 4: no module-level STATE_FILE ----------------------------------


def test_state_store_does_not_expose_module_level_state_file():
    # The path is instance-owned; importing the module must not advertise
    # a canonical STATE_FILE constant.
    import forgejo_to_github.state as state_module

    assert not hasattr(state_module, "STATE_FILE"), (
        "StateStore should own its path; module-level STATE_FILE is not allowed."
    )


def test_state_store_uses_instance_path_not_module_global(tmp_path):
    """Two stores with different paths must not interfere."""
    path_a = tmp_path / "a.json"
    path_b = tmp_path / "b.json"

    StateStore(path_a, "owner/source", "owner/target").save(
        repo_created=True, git_pushed=False, migrated={1: 1}
    )
    StateStore(path_b, "owner/source", "owner/target").save(
        repo_created=False, git_pushed=True, migrated={2: 2}
    )

    assert json.loads(path_a.read_text())["repo_created"] is True
    assert json.loads(path_b.read_text())["repo_created"] is False
    assert json.loads(path_a.read_text())["migrated"] == {"1": 1}
    assert json.loads(path_b.read_text())["migrated"] == {"2": 2}


# --- guard rail: signature sanity --------------------------------------------


def test_state_store_constructor_takes_path_source_target():
    """Constructor signature is locked: (state_path, source, target)."""
    import inspect

    sig = inspect.signature(StateStore.__init__)
    params = list(sig.parameters)
    assert params[:3] == ["self", "state_path", "source"]
    assert "target" in params


def test_save_signature_locked():
    """save() signature is locked: (repo_created, git_pushed, migrated)."""
    import inspect

    sig = inspect.signature(StateStore.save)
    params = list(sig.parameters)
    assert params[:1] == ["self"]
    for name in ("repo_created", "git_pushed", "migrated"):
        assert name in params, f"save() must accept {name!r}"
