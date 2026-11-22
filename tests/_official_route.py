# SPDX-License-Identifier: GPL-3.0-or-later
"""The shared installer/launcher route, as a test vehicle (#1452).

``WineGameBackend``'s installer route (download an installer, run it in the prefix,
find and start the launcher it leaves) still serves the ``launcher-*`` kinds. Many
older tests exercised it through Arknights, which used it when no manifest was
configured. #1452 retired that route for Arknights, so those tests now run against
this class instead: Arknights' Wine/Proton set-up with the old "official when there
is no manifest" switch, and nothing else. It is never registered as a kind.
"""

from __future__ import annotations

from nadare.backends import winegame
from nadare.backends.arknights import ArknightsBackend
from nadare.backends.winegame import WineGameBackend


class OfficialRouteBackend(ArknightsBackend):
    """Arknights as it was before #1452: official launcher route without a manifest."""

    def direct_mode(self) -> bool:
        return bool(self.manifest_source())

    def secure_mode_enabled(self) -> bool:
        # The official route never ran in the Sandbox (#1450).
        return self.direct_mode() and WineGameBackend.secure_mode_enabled(self)

    def _installer_host_trusted(self, url: str) -> bool:
        from nadare.core import yostar

        return yostar.base_url_trusted(url)

    # Arknights is a DirectBackend game since #1452 3d: the route's own lifecycle and
    # launcher search come from the shared base, as they did before.
    launcher_names = winegame.LAUNCHER_NAMES
    # Looked up at call time, so a test that patches WineGameBackend's method reaches it.
    def install(self) -> None:
        WineGameBackend.install(self)

    def update(self) -> None:
        WineGameBackend.update(self)

    def launch(self) -> int:
        return WineGameBackend.launch(self)


__all__ = ["OfficialRouteBackend"]
