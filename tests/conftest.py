# SPDX-License-Identifier: GPL-3.0-or-later
"""Hermetic test configuration for Nadare.

Every test gets a throw-away HOME and XDG tree so nothing touches the real
user's config, state, cache or downloads. ``src`` is put on ``sys.path`` so the
suite also runs without ``PYTHONPATH=src``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Force, not setdefault: a desktop session exports QT_QPA_PLATFORM and the GUI
# tests would otherwise open real windows (and behave differently).
os.environ["QT_QPA_PLATFORM"] = "offscreen"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture(autouse=True)
def _isolated_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Redirect HOME/XDG and force the plaintext secret fallback.

    The redirect is applied for *every* test, so ``nadare.core.paths`` (which
    reads the environment lazily) always resolves under ``tmp_path``.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))

    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg / "cache"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))

    # Never talk to a real keyring during tests; use the documented fallback.
    monkeypatch.setenv("NADARE_NO_KEYRING", "1")

    # The one-time "unofficial launcher" notice and the big-download question are
    # modal; tests opt in to them explicitly.
    monkeypatch.setenv("NADARE_ASSUME_NOTICES", "1")

    # No test may reach Nadare's real index or release page by default.
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    monkeypatch.setenv("NADARE_RELEASES_API", "")
    monkeypatch.delenv("CZN_MANIFEST_INDEX", raising=False)
    monkeypatch.delenv("YOSTAR_MANIFEST_INDEX", raising=False)
    monkeypatch.delenv("YOSTAR_MEDIA_FILE", raising=False)

    # The CZN 32-bit-libs preflight reads the real ldconfig cache; tests must
    # not depend on the host (tests that exercise it patch ``missing_32bit``).
    from nadare.core import hostlibs

    if request.module.__name__ != "test_hostlibs":
        monkeypatch.setattr(hostlibs, "missing_32bit", lambda: [])
        monkeypatch.setattr(hostlibs, "missing_gl", lambda: [])

    # Installers need a display (#437); CI has none. Tests that exercise the
    # headless refusal remove it again.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        monkeypatch.setenv("DISPLAY", ":99")

    # Logging may already have been configured at import time (with the real
    # state dir). Tear it down and re-point the file handler at this test's
    # tmp tree so the suite never appends to the user's real log.
    from nadare.core import logging as nadare_logging

    nadare_logging.reset()
    nadare_logging.setup(filename=tmp_path / "nadare.log")
    yield
    nadare_logging.reset()


@pytest.fixture(autouse=True)
def _http_open_follows_patched_urlopen(monkeypatch):
    """Tests that fake ``urllib.request.urlopen`` also fake ``download.http_open``.

    The WebView2 / VC++ downloads go through ``download.http_open`` (so a
    per-hop redirect guard applies); older tests stub ``urlopen`` instead.
    """
    import urllib.error
    import urllib.request

    from nadare.core import download

    real_http_open = download.http_open
    real_urlopen = urllib.request.urlopen

    def http_open(url, *, headers=None, timeout=download.DEFAULT_TIMEOUT, redirect_allowed=None):
        if urllib.request.urlopen is real_urlopen:
            return real_http_open(
                url, headers=headers, timeout=timeout, redirect_allowed=redirect_allowed
            )
        try:
            return urllib.request.urlopen(url, timeout=timeout)
        except download.DownloadError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise download.DownloadError(f"cannot reach {url}: {exc}") from exc

    monkeypatch.setattr(download, "http_open", http_open)


@pytest.fixture(autouse=True)
def _restore_backend_registry():
    """Tests that ``register()`` a fake backend must not leak it to other tests."""
    from nadare.core import backends

    with backends._REGISTRY_LOCK:
        saved = dict(backends._REGISTRY)
    yield
    with backends._REGISTRY_LOCK:
        backends._REGISTRY.clear()
        backends._REGISTRY.update(saved)
