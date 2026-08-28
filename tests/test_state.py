import json

import f2gh


def test_save_and_load_state_round_trip(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(f2gh, "STATE_FILE", str(state_path))

    f2gh.save_state(
        "owner/source",
        "owner/target",
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
    assert f2gh.load_state("owner/source", "owner/target") == {
        "repo_created": True,
        "git_pushed": False,
        "migrated": {12: 34},
    }
