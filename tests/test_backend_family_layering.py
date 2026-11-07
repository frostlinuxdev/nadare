# SPDX-License-Identifier: GPL-3.0-or-later
"""Layering rules for the Arknights-family backends (issue #741).

The neutral base (backends/winegame.py) must not know about the Yostar manifest code; only the
Yostar backend handles its manifest error. These replace a table of hashes of every attribute,
which every backend change had to edit and which caused merge conflicts.
"""

from __future__ import annotations

import inspect

from nadare.backends import arknights, direct, launcher, stellasora
from nadare.backends.winegame import WineGameBackend


def test_the_direct_and_launcher_backends_do_not_inherit_the_yostar_backend():
    yostar_backend = arknights.ArknightsBackend
    assert issubclass(yostar_backend, WineGameBackend)
    for cls in (direct.DirectBackend, launcher.LauncherBackend):
        assert issubclass(cls, WineGameBackend), cls
        assert not issubclass(cls, yostar_backend), cls
    # Stella Sora is a Yostar-manifest game: it keeps the Yostar backend as its parent.
    assert issubclass(stellasora.StellaSoraBackend, yostar_backend)


def test_the_neutral_base_does_not_depend_on_the_yostar_module():
    import nadare.backends.winegame as winegame

    text = inspect.getsource(winegame)
    assert "yostar." not in text
    assert "import yostar" not in text
    assert "yostar import" not in text


def test_the_yostar_backend_does_not_carry_its_own_copy_of_the_shared_methods():
    for name in ("repair", "verify", "_record_synced_version"):
        assert name not in vars(arknights.ArknightsBackend), name


def test_only_the_yostar_backend_handles_the_manifest_error():
    from nadare.core import yostar

    assert arknights.ArknightsBackend._manifest_errors == (yostar.ManifestError,)
    assert WineGameBackend._manifest_errors == ()
    assert direct.DirectBackend._manifest_errors == ()
    assert launcher.LauncherBackend._manifest_errors == ()
