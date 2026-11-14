# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path

from nadare.core.config import Config


def test_a_config_that_vanishes_before_the_read_is_treated_as_missing(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    real_read_text = Path.read_text

    def gone(self, *args, **kwargs):
        if str(self) == str(path):
            raise FileNotFoundError(2, "No such file or directory", str(path))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", gone)
    cfg = Config.load(path)  # currently raises ConfigReadError
    assert cfg.profiles == {}
