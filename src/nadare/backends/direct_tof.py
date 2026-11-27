# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Tower of Fantasy, installed from its public file list."""

from pathlib import Path

from ..core import nte_launcher
from ..core.backends import BackendError
from ..core.diskspace import human_size
from ..core.sources import tof
from .direct import GB, DirectBackend, game_subdir, publish_game_exports

#: The finished install (the Store's figure).
TOF_INSTALLED_BYTES = 129_327_413_971
#: What a fresh install downloads.
TOF_DOWNLOAD_BYTES = int(132.8 * GB)


class TofBackend(DirectBackend):
    game_process_names = ("QRSL.exe",)
    kind = "direct-tof"
    game_tag = "tof"
    display_name = "Tower of Fantasy"
    install_subdir = game_subdir("TowerOfFantasy")
    direct_exe = tof.EXE
    launcher_names = (direct_exe,)
    install_size_hint = TOF_INSTALLED_BYTES
    download_size_hint = TOF_DOWNLOAD_BYTES
    #: No unpacked archive is kept beyond the downloads, so the download figure is the floor.
    install_space_hint = TOF_DOWNLOAD_BYTES
    plan_factory = staticmethod(tof.resolve)
    #: The launcher has an embedded browser for login and news; as with NTE, XWayland is the
    #: safe default. ``display: wayland`` still wins.
    default_display = "x11"
    page_summary = (
        "Perfect World's launcher starts with the game as a small bar and starts it by "
        "itself; that is expected."
    )
    page_notice = (
        "Installs straight from Perfect World's servers "
        f"({human_size(TOF_INSTALLED_BYTES)} installed; allow about "
        f"{human_size(TOF_DOWNLOAD_BYTES)} of downloads). The game's sign-in only works while "
        "Perfect World's launcher is running, so Nadare also fetches the launcher files "
        "(7-Zip needed) and starts it "
        "with /autoplay: the launcher collapses to a small bar and starts the game by itself. "
        "On the very first start the launcher updates itself once (a minute or two).",
        "https://www.toweroffantasy-global.com/",
        "Official site",
    )

    def _direct_argv(self, target: Path) -> list[str]:
        try:
            launcher = nte_launcher.ensure_tof_launcher(
                self.install_dir(), data_dir=self._data_dir()
            )
        except nte_launcher.TofLauncherError as exc:
            raise BackendError(f"cannot prepare the Tower of Fantasy launcher: {exc}") from exc
        self.info("ToF: starting the publisher launcher (/autoplay starts the game)")
        return [str(launcher), "/launcher", "/autoplay", *super()._direct_argv(target)[1:]]


publish_game_exports(__name__, globals())
