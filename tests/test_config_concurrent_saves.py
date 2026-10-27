# SPDX-License-Identifier: GPL-3.0-or-later
"""Two processes saving one config must not lose each other's changes (#579, #615)."""

from __future__ import annotations

import json
import threading
import time

from nadare.core.config import Config


def test_save_keeps_a_top_level_key_another_process_added(tmp_path):
    path = tmp_path / "config.json"
    first = Config.load(path)
    first.add_profile("a", backend="wine", title="A")
    first.save()
    # The second process (loaded earlier than the key existed) sets a new key.
    second = Config.load(path)
    second.data["unofficial_notice_ack"] = True
    second.save()
    first.data["language"] = "de"  # unrelated local change, then save
    first.save()
    on_disk = json.loads(path.read_text())
    assert on_disk["unofficial_notice_ack"] is True
    assert on_disk["language"] == "de"


def test_a_locally_added_top_level_key_wins_over_nothing_on_disk(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.data["new_key"] = 1
    cfg.save()
    assert json.loads(path.read_text())["new_key"] == 1


def test_a_locally_deleted_known_key_stays_deleted(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    cfg.data["extra"] = "x"
    cfg.save()
    again = Config.load(path)
    del again.data["extra"]
    again.save()
    assert "extra" not in json.loads(path.read_text())


def test_symlinked_and_real_config_share_one_lock(tmp_path):
    real = tmp_path / "real.json"
    link = tmp_path / "link.json"
    Config.load(real).save()
    link.symlink_to(real)
    via_real = Config.load(real)
    via_link = Config.load(link)
    # Hold the lock through the symlinked name; the real-path writer must wait.
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with via_link._file_lock():
            entered.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(5)
    started = time.monotonic()
    threading.Timer(0.3, release.set).start()
    with via_real._file_lock():
        waited = time.monotonic() - started
    thread.join()
    assert waited >= 0.25
