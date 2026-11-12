import json

from nadare.core import config as config_mod
from nadare.core.config import Config


def test_a_migration_that_drops_a_profile_is_not_undone_by_save(tmp_path, monkeypatch):
    def merge_old_into_new(raw):
        raw["profiles"].pop("old", None)
        raw["profiles"]["new"] = {"backend": "wine", "title": "New"}

    monkeypatch.setattr(config_mod, "SCHEMA", 2)
    monkeypatch.setattr(config_mod, "MIGRATIONS", {1: merge_old_into_new})
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "active_profile": "old",
                "profiles": {"old": {"backend": "wine", "prefix": "/p"}},
            }
        ),
        encoding="utf-8",
    )

    cfg = Config.load(path)
    assert "old" not in cfg.profiles
    cfg.save()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert "old" not in on_disk["profiles"]
