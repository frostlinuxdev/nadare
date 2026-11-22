# SPDX-License-Identifier: GPL-3.0-or-later
"""The undo of a Set up change is kept on disk, so it still works after Nadare restarts."""

from __future__ import annotations

import errno
import json
import subprocess
import sys
from pathlib import Path

import pytest

from nadare.core import steamtools, steamundo, steamwayland

ENTRY = steamtools.CompatEntry("3224770", {"name": "proton_experimental", "config": ""}, "BLOCK")


def test_a_runner_undo_comes_back_exactly(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    action, payload = steamundo.load("3224770", state_dir=tmp_path)
    assert action == "set_runner"
    assert payload == ENTRY and payload.text == "BLOCK" and payload.tool == "proton_experimental"


def test_a_game_that_had_no_runner_setting_comes_back_as_none(tmp_path):
    steamundo.save("3224770", "set_runner", None, state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path) == ("set_runner", None)


def test_what_nadare_wrote_is_saved_with_a_launch_option_undo(tmp_path):
    undo = steamwayland.LaunchUndo("mangohud %command%", written="X=1 mangohud %command%")
    steamundo.save("5019180", "set_wayland", undo, state_dir=tmp_path)
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_wayland", undo)


def test_a_launch_option_undo_comes_back_exactly(tmp_path):
    for previous in ("mangohud %command%", None, ""):
        undo = steamwayland.LaunchUndo(previous)
        steamundo.save("5019180", "set_wayland", undo, state_dir=tmp_path)
        assert steamundo.load("5019180", state_dir=tmp_path) == (
            "set_wayland",
            steamwayland.LaunchUndo(previous),
        )


def test_each_game_keeps_its_own_undo_and_clear_removes_only_that_one(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    steamundo.save("5019180", "set_wayland", steamwayland.LaunchUndo(None), state_dir=tmp_path)
    steamundo.clear("3224770", state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path) is None
    assert steamundo.load("5019180", state_dir=tmp_path) is not None
    steamundo.clear("999", state_dir=tmp_path)  # nothing saved: not an error


def test_the_newest_change_is_the_one_offered_first(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    steamundo.save("3224770", "set_wayland", steamwayland.LaunchUndo("x"), state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path)[0] == "set_wayland"


def test_a_missing_or_damaged_file_is_just_no_undo(tmp_path):
    assert steamundo.load("3224770", state_dir=tmp_path) is None
    for text in (
        "",
        "not json",
        "[]",
        '{"3224770": 5}',
        '{"3224770": {"action": "x"}}',
        '{"3224770": {"action": "set_runner", "payload": {"kind": "weird"}}}',
    ):
        (tmp_path / steamundo.FILE).write_text(text)
        assert steamundo.load("3224770", state_dir=tmp_path) is None
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)  # heals over the damage
    assert steamundo.load("3224770", state_dir=tmp_path) is not None


def test_only_the_two_undoable_steps_are_saved(tmp_path):
    steamundo.save("3224770", "unlock_h264", None, state_dir=tmp_path)
    assert steamundo.load("3224770", state_dir=tmp_path) is None
    assert not (tmp_path / steamundo.FILE).exists()


def test_the_file_is_plain_json_written_whole(tmp_path):
    steamundo.save("3224770", "set_runner", ENTRY, state_dir=tmp_path)
    data = json.loads((tmp_path / steamundo.FILE).read_text())
    assert [entry["action"] for entry in data["3224770"]] == ["set_runner"]  # a stack, #1031
    assert not list(tmp_path.glob("*.tmp*")) and not list(tmp_path.glob(".*"))


# ---- an undo belongs to the Steam account it was made for (#894) -------------------------------
def test_the_account_file_is_remembered_with_the_undo(tmp_path):
    undo = steamwayland.LaunchUndo("--previous-A")
    where = "/steam/userdata/1/config/localconfig.vdf"
    steamundo.save("5019180", "set_wayland", undo, where=where, state_dir=tmp_path)
    assert steamundo.where("5019180", state_dir=tmp_path) == where
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_wayland", undo)


def test_an_undo_fits_the_account_it_was_saved_for(tmp_path):
    undo = steamwayland.LaunchUndo("x")
    steamundo.save("1", "set_wayland", undo, where="/a/localconfig.vdf", state_dir=tmp_path)
    assert steamundo.fits("1", "/a/localconfig.vdf", state_dir=tmp_path)
    assert not steamundo.fits("1", "/b/localconfig.vdf", state_dir=tmp_path)


def test_an_older_launch_option_undo_without_an_account_is_not_trusted(tmp_path):
    steamundo.save("1", "set_wayland", steamwayland.LaunchUndo("x"), state_dir=tmp_path)
    assert not steamundo.fits("1", "/a/localconfig.vdf", state_dir=tmp_path)


def test_a_runner_undo_fits_only_the_steam_install_it_was_saved_for(tmp_path):
    # #970: native Steam and Flatpak Steam each have their own config.vdf.
    steamundo.save("1", "set_runner", ENTRY, where="/native/config.vdf", state_dir=tmp_path)
    assert steamundo.fits("1", "/native/config.vdf", state_dir=tmp_path)
    assert not steamundo.fits("1", "/flatpak/config.vdf", state_dir=tmp_path)


def test_an_older_runner_undo_without_an_install_is_not_trusted(tmp_path):
    steamundo.save("1", "set_runner", ENTRY, state_dir=tmp_path)
    assert not steamundo.fits("1", "/anything", state_dir=tmp_path)


def test_no_undo_fits_nothing(tmp_path):
    assert not steamundo.fits("1", "/a/localconfig.vdf", state_dir=tmp_path)


# ---- a second change no longer throws the first one's undo away (#1031) --------------------
def test_undos_stack_so_each_change_can_be_taken_back_in_turn(tmp_path):
    runner = steamtools.CompatEntry("5019180", {"name": "proton_9"}, "text")
    launch = steamwayland.LaunchUndo("mangohud %command%", written="X=1 mangohud %command%")
    steamundo.save("5019180", "set_runner", runner, where="/s/config.vdf", state_dir=tmp_path)
    steamundo.save("5019180", "set_wayland", launch, where="/s/local.vdf", state_dir=tmp_path)
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_wayland", launch)
    assert steamundo.where("5019180", state_dir=tmp_path) == "/s/local.vdf"
    steamundo.clear("5019180", state_dir=tmp_path)  # the newest was undone
    assert steamundo.load("5019180", state_dir=tmp_path) == ("set_runner", runner)
    assert steamundo.where("5019180", state_dir=tmp_path) == "/s/config.vdf"
    steamundo.clear("5019180", state_dir=tmp_path)
    assert steamundo.load("5019180", state_dir=tmp_path) is None


def test_an_undo_file_from_before_the_stack_still_loads(tmp_path):
    import json

    (tmp_path / steamundo.FILE).write_text(
        json.dumps(
            {
                "7": {
                    "action": "set_wayland",
                    "payload": {"kind": "launch", "previous": None},
                    "where": "/s/local.vdf",
                }
            }
        )
    )
    assert steamundo.load("7", state_dir=tmp_path) == ("set_wayland", steamwayland.LaunchUndo(None))
    steamundo.save("7", "set_wayland", steamwayland.LaunchUndo("a"), where="/x", state_dir=tmp_path)
    steamundo.clear("7", state_dir=tmp_path)
    assert steamundo.load("7", state_dir=tmp_path) == ("set_wayland", steamwayland.LaunchUndo(None))


def test_the_stack_keeps_only_the_last_few(tmp_path):
    for n in range(12):
        steamundo.save(
            "9", "set_wayland", steamwayland.LaunchUndo(str(n)), where="/w", state_dir=tmp_path
        )
    seen = []
    while (got := steamundo.load("9", state_dir=tmp_path)) is not None:
        seen.append(got[1].previous)
        steamundo.clear("9", state_dir=tmp_path)
    assert seen == [str(n) for n in range(11, 11 - steamundo.KEEP, -1)]


# --- #1491: one cross-process read-modify-write boundary for save and clear -----------------

_SRC = Path(steamundo.__file__).resolve().parents[2]

# A second process that pauses right after it read the undo file, inside whatever boundary
# save()/clear() hold at that moment. If there is no lock, the parent's write lands while the
# child holds a stale snapshot, and the child's replace erases it.
_CHILD = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from nadare.core import steamundo
real = steamundo._read
def paused(state):
    snapshot = real(state)
    print('snapshot-read', flush=True)
    assert sys.stdin.readline().strip() == 'continue'
    return snapshot
steamundo._read = paused
state = Path(sys.argv[2])
if sys.argv[3] == 'save':
    steamundo.save('111', 'set_runner', None, where='/synthetic/config.vdf', state_dir=state)
else:
    steamundo.clear('111', state_dir=state)
print('child-done', flush=True)
"""


def _overlap(tmp_path, child_op, parent_op):
    """Start the child, wait until it has read, run ``parent_op`` from a thread, then let the
    child finish. With a lock, the parent blocks until the child is done (it must not finish
    first); without one it finishes at once and the child's stale write wins."""
    import threading

    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(_SRC), str(tmp_path), child_op],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    errors: list[BaseException] = []

    def run():
        try:
            parent_op()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    worker = threading.Thread(target=run)
    try:
        assert proc.stdout.readline().strip() == "snapshot-read"
        worker.start()
        worker.join(0.5)  # the parent must still be waiting for the child's boundary
        parent_waited = worker.is_alive()
        out, err = proc.communicate("continue\n", timeout=30)
        assert proc.returncode == 0, err
        assert "child-done" in out
        worker.join(30)
        assert not worker.is_alive()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert not errors, errors
    return parent_waited


def test_two_processes_saving_at_once_keep_both_games_undo(tmp_path):
    waited = _overlap(
        tmp_path,
        "save",
        lambda: steamundo.save(
            "222", "set_runner", None, where="/synthetic/config.vdf", state_dir=tmp_path
        ),
    )
    assert waited, "the second save did not wait for the first one's read-modify-write"
    assert steamundo.load("111", state_dir=tmp_path) is not None
    assert steamundo.load("222", state_dir=tmp_path) is not None


def test_a_clear_running_alongside_a_save_does_not_erase_that_save(tmp_path):
    steamundo.save("111", "set_runner", None, where="/synthetic/config.vdf", state_dir=tmp_path)
    waited = _overlap(
        tmp_path,
        "clear",
        lambda: steamundo.save(
            "222", "set_runner", ENTRY, where="/synthetic/config.vdf", state_dir=tmp_path
        ),
    )
    assert waited
    assert steamundo.load("111", state_dir=tmp_path) is None  # the clear happened
    assert steamundo.load("222", state_dir=tmp_path) == ("set_runner", ENTRY)


def test_two_processes_pushing_on_the_same_game_keep_both_entries(tmp_path):
    first = steamwayland.LaunchUndo("A", "B")
    waited = _overlap(
        tmp_path,
        "save",
        lambda: steamundo.save(
            "111", "set_wayland", first, where="/synthetic/localconfig.vdf", state_dir=tmp_path
        ),
    )
    assert waited
    stack = json.loads((tmp_path / steamundo.FILE).read_text())["111"]
    assert [entry["action"] for entry in stack] == ["set_runner", "set_wayland"]  # child first


def test_the_lock_is_a_separate_file_so_the_atomic_replace_does_not_drop_it(tmp_path):
    steamundo.save("111", "set_runner", None, where="/x", state_dir=tmp_path)
    assert (tmp_path / (steamundo.FILE + ".lock")).exists()
    assert json.loads((tmp_path / steamundo.FILE).read_text())  # the store itself is plain JSON


@pytest.mark.parametrize("code", [errno.ENOLCK, errno.EOPNOTSUPP])
def test_a_filesystem_without_locking_still_saves_the_undo(tmp_path, monkeypatch, code):
    def unsupported(*_):
        raise OSError(code, "no locks here")

    monkeypatch.setattr(steamundo.fcntl, "flock", unsupported)
    steamundo.save("111", "set_runner", None, where="/x", state_dir=tmp_path)
    assert steamundo.load("111", state_dir=tmp_path) == ("set_runner", None)
    steamundo.clear("111", state_dir=tmp_path)
    assert steamundo.load("111", state_dir=tmp_path) is None


def test_any_other_lock_error_changes_nothing_and_is_raised(tmp_path, monkeypatch):
    steamundo.save("111", "set_runner", None, where="/x", state_dir=tmp_path)
    before = (tmp_path / steamundo.FILE).read_text()

    def broken(*_):
        raise OSError(errno.EIO, "lock failed")

    monkeypatch.setattr(steamundo.fcntl, "flock", broken)
    with pytest.raises(OSError):
        steamundo.save("222", "set_runner", None, where="/x", state_dir=tmp_path)
    with pytest.raises(OSError):
        steamundo.clear("111", state_dir=tmp_path)
    assert (tmp_path / steamundo.FILE).read_text() == before
