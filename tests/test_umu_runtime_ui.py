# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime storage review and Ubuntu upgrade offer (#1443)."""

from PyQt6.QtWidgets import QApplication

from nadare.core import paths, umu, userns
from nadare.core.config import Config
from nadare.gui.main_window_parts.cleanup import CleanupMixin
from nadare.gui.userns_dialog import UsernsDialog


def test_runtime_review_requires_tick_and_confirmation(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    own = paths.data_dir() / "umu-runtime"
    shared = umu.shared_runtime_roots()[0]
    for root in (own, shared):
        root.mkdir(parents=True)
        (root / "data").write_text("runtime")
    from nadare.gui.disk_space_panel import RuntimeFoldersDialog

    dialog = RuntimeFoldersDialog(
        Config.load(paths.config_file()), [(own, 7), (shared, 3 * 1024**3)]
    )
    assert not dialog.delete_button.isEnabled()
    assert any("Shared umu runtime (other apps may use it)" in box.text() for box in dialog.boxes)
    dialog.boxes[1].setChecked(True)
    assert dialog.delete_button.isEnabled()
    monkeypatch.setattr(dialog, "_confirm", lambda items: False)
    dialog._delete_ticked()
    assert shared.exists()
    dialog._delete(dialog._ticked(), {}, [])
    app.processEvents()
    assert not shared.exists()
    assert own.exists()
    assert "3 GiB" in dialog.status.text()
    dialog.close()


def test_runtime_removal_rechecks_busy_and_links(monkeypatch, tmp_path):
    from nadare.core import uninstall_all

    root = umu.shared_runtime_roots()[0]
    root.mkdir(parents=True)
    (root / "data").write_text("keep")
    monkeypatch.setattr(uninstall_all, "_running_from", lambda root: ["game"])
    assert not umu.remove_runtime(root)
    assert root.exists()
    monkeypatch.setattr(uninstall_all, "_running_from", lambda root: [])
    other = tmp_path / "other"
    root.rename(other)
    root.symlink_to(other, target_is_directory=True)
    assert not umu.remove_runtime(root)
    assert (other / "data").exists()


def test_upgrade_offer_uses_same_fix_dialog_once_per_start(monkeypatch):
    import nadare.gui.userns_dialog as module

    calls = []

    class Dialog:
        def __init__(self, message, parent):
            calls.append(message)

        def exec(self):
            pass

    monkeypatch.setattr(module, "UsernsDialog", Dialog)
    monkeypatch.setattr(userns, "runtime_profile_outdated", lambda: True)
    window = type("Window", (), {})()
    CleanupMixin._offer_runtime_profile_update(window)
    CleanupMixin._offer_runtime_profile_update(window)
    assert len(calls) == 1
    assert "Nadare moved its runtime" in calls[0]


def test_moved_runtime_dialog_explains_fallback(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(userns, "can_fix", lambda: True)
    dialog = UsernsDialog(userns.RUNTIME_MOVED_MESSAGE + "\n" + userns.hint())
    assert "Nadare moved its runtime" in dialog.summary.text()
    assert "shared runtime" in dialog.summary.text()
    assert "cannot start" not in dialog.summary.text()
    assert dialog.fix_button.text() == "Fix it"
    app.processEvents()
    dialog.close()
