# SPDX-License-Identifier: GPL-3.0-or-later
"""The runner unpacking says so, instead of leaving the download frozen at 100%.

A b29 launch downloaded the Proton runner and then sat at 100% for a long time: the
archive's extraction emitted only a header "status" (the small pill), so the step text
still read like the transfer that had already finished.

The page rule is general: any step that follows a finished download is busy work with
no percentage of its own, and the launch itself ends that phase.
"""

from __future__ import annotations

import os
import tarfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from nadare.core import runners
from nadare.core.events import Event

TAG = runners.RUNNERS["dwproton"].tested[0]
NAME = TAG + "-x86_64"


@pytest.fixture(scope="module")
def qapp():
    try:
        from PyQt6.QtWidgets import QApplication
    except Exception:  # noqa: BLE001
        pytest.skip("PyQt6 is not importable in this interpreter")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def runners_dir(tmp_path, monkeypatch):
    root = tmp_path / "runners"
    root.mkdir()
    monkeypatch.setattr(runners.paths, "runners_dir", lambda: root)
    # The host's free space and its own Proton builds must not decide this test.
    monkeypatch.setattr(runners.diskspace, "check_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(runners, "installed_pinned", lambda *args, **kwargs: None)
    monkeypatch.setattr(runners, "steam_runners", lambda: [])
    return root


def _tarball(tmp_path) -> bytes:
    from runner_layout import make_runner

    tree = make_runner(tmp_path / "source" / NAME)
    path = tmp_path / (NAME + ".tar.gz")
    with tarfile.open(path, "w:gz") as out:
        out.add(tree, arcname=NAME)
    return path.read_bytes()


def _fake_download(body: bytes):
    def download(url, dest, **kwargs):
        dest.write_bytes(body)
        kwargs["emit"](
            Event(
                "progress",
                "download: 10 MB / 10 MB",
                1.0,
                {"stage": "download", "done": 10_000_000, "total": 10_000_000},
            )
        )
        return runners.download.DownloadResult(dest, len(body), "a" * 64, "b" * 128)

    return download


def _page(qapp):
    from nadare.gui.game_view import GameView

    page = GameView()
    page.set_profile("sample", {"backend": "direct-strinova", "title": "Sample"})
    page.set_busy("launch")
    return page


def _close(page, qapp) -> None:
    page._timer.stop()
    page.deleteLater()
    qapp.processEvents()


def _events_after_download(events: list[Event]) -> list[Event]:
    finished = max(index for index, event in enumerate(events) if event.progress == 1.0)
    return events[finished + 1 :]


def test_the_real_runner_install_names_the_unpack_and_the_page_shows_it(
    runners_dir, tmp_path, monkeypatch, qapp
):
    body = _tarball(tmp_path)
    monkeypatch.setattr(runners.download, "download", _fake_download(body))
    events: list[Event] = []

    dest = runners.install("dwproton", emit=events.append)

    assert dest.name == NAME
    after = _events_after_download(events)
    steps = [event for event in after if event.kind == "step"]
    assert steps, f"no step after the download reached 100%: {[e.kind for e in after]}"
    assert steps[0].data["stage"] == "extract"
    assert "unpack" in steps[0].message.lower(), steps[0].message

    page = _page(qapp)
    try:
        for event in events:
            page.append_event(event)
        assert page.step_label.text() == steps[0].message
        # A busy bar: the transfer is over, and the page must not look stuck at 100%.
        assert page.progress.maximum() == 0
        assert not page.progress.isHidden()

        page.append_event(Event("step", "Sample: launching Sample.exe"))
        assert page.progress.isHidden()
    finally:
        _close(page, qapp)


def test_a_step_after_a_finished_download_is_busy_without_any_stage_name(qapp):
    """The rule is not the runner's "extract" stage: any post-download step is busy work."""
    page = _page(qapp)
    try:
        page.append_event(
            Event(
                "progress",
                "download: 10 MB / 10 MB",
                1.0,
                {"stage": "download", "done": 10_000_000, "total": 10_000_000},
            )
        )
        assert page.progress.maximum() == 100
        page.append_event(Event("step", "Checking the download…"))
        assert page.step_label.text() == "Checking the download…"
        assert page.progress.maximum() == 0
        assert not page.progress.isHidden()
    finally:
        _close(page, qapp)


@pytest.mark.parametrize(
    "end",
    [
        Event("step", "Sample: launching Sample.exe"),
        Event("game_running", "Sample is running"),
    ],
)
def test_the_launching_step_ends_the_post_download_busy_bar(qapp, end):
    page = _page(qapp)
    try:
        page.append_event(
            Event(
                "progress",
                "download: 10 MB / 10 MB",
                1.0,
                {"stage": "download", "done": 10_000_000, "total": 10_000_000},
            )
        )
        page.append_event(Event("step", "Unpacking the runner…", data={"stage": "extract"}))
        assert not page.progress.isHidden()
        page.append_event(end)
        assert page.progress.isHidden()
    finally:
        _close(page, qapp)
