"""Stage-06 alignment: state round-trip via ``StateStore``.

Legacy ``f2gh.save_state`` / ``f2gh.load_state`` / ``f2gh.STATE_FILE`` are
removed in stage 06. This file is rewritten to exercise the extracted
``forgejo_to_github.state.StateStore`` directly.

Preserved contract (from original ``test_save_and_load_state_round_trip``):
- Integer ``migrated`` keys are serialized as JSON strings on disk.
- ``load()`` restores integer keys and the ``repo_created`` / ``git_pushed``
  flags.
- The on-disk JSON shape remains ``{"source","target","repo_created",
  "git_pushed","migrated"}``.

Removed/redundant coverage (already directly covered by
``tests/test_state_store.py`` and documented per ``06-cli-wiring.md`` §6.2):
- No duplication of ``test_load_returns_default_state_when_file_absent``,
  ``test_load_ignores_checkpoint_with_mismatched_source``,
  ``test_save_persists_all_fields_and_uses_atomic_replace``, etc.
  Those are exercised exhaustively in ``test_state_store.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from forgejo_to_github.state import StateStore


def test_save_and_load_state_round_trip(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = StateStore(state_path, "owner/source", "owner/target")

    store.save(
        repo_created=True,
        git_pushed=False,
        migrated={12: 34},
    )

    assert json.loads(state_path.read_text()) == {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": True,
        "git_pushed": False,
        "migrated": {"12": 34},
    }
    # StateStore.load() returns the dict form with int keys (legacy shape).
    assert store.load() == {
        "source": "owner/source",
        "target": "owner/target",
        "repo_created": True,
        "git_pushed": False,
        "migrated": {12: 34},
    }
