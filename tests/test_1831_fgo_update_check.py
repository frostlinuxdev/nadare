# SPDX-License-Identifier: GPL-3.0-or-later
"""#1831: FGO's background update check runs and the page learns of an update.

``lepton-fgo check-update`` existed, but the backend never called it, so FGO never showed
Update. ``check_update()`` (background, never raises) now caches the answer for the
installed version; ``status()`` reads only that cache, never the network.
"""

from __future__ import annotations

from _backend_helpers import make_context

from nadare.backends.fgo.backend import FgoLeptonBackend
from nadare.backends.fgo.lepton import LeptonError


class FakeAdapter:
    installed = True

    def __init__(self, version: str = "2.91.0", report: dict | None = None, error: bool = False):
        self.version = version
        self.report = report
        self.error = error
        self.checks = 0

    def status(self, ctx=None):  # noqa: ANN001
        return {
            "context": "fgo",
            "container": "lepton-fgo (stopped)",
            "fgo": self.version,
            "venus": "stopped",
            "avahi": "stopped",
        }

    def check_update(self, ctx=None):  # noqa: ANN001
        self.checks += 1
        if self.error:
            raise LeptonError("network down")
        return dict(self.report or {})


AVAILABLE = {"installed": "2.91.0", "newest": "2.93.0", "available": True}
UP_TO_DATE = {"installed": "2.91.0", "newest": "2.91.0", "available": False}


def _backend(tmp_path, adapter: FakeAdapter, **cfg) -> FgoLeptonBackend:
    ctx = make_context(tmp_path, "fgo-lepton", {"prefix": str(tmp_path / "prefix")})
    ctx.config.data.update(cfg)
    backend = FgoLeptonBackend(ctx)
    backend.adapter = adapter  # type: ignore[assignment]
    return backend


def test_an_available_update_shows_on_the_page(tmp_path):
    adapter = FakeAdapter(report=AVAILABLE)
    backend = _backend(tmp_path, adapter)
    backend.check_update()
    assert adapter.checks == 1
    assert backend.status().data.get("update_available") is True


def test_up_to_date_shows_no_update(tmp_path):
    backend = _backend(tmp_path, FakeAdapter(report=UP_TO_DATE))
    backend.check_update()
    assert backend.status().data.get("update_available") is False


def test_a_new_backend_object_still_knows_the_last_answer(tmp_path):
    _backend(tmp_path, FakeAdapter(report=AVAILABLE)).check_update()
    fresh = _backend(tmp_path, FakeAdapter())
    assert fresh.status().data.get("update_available") is True


def test_status_never_runs_the_check(tmp_path):
    adapter = FakeAdapter(report=AVAILABLE)
    backend = _backend(tmp_path, adapter)
    backend.status()
    assert adapter.checks == 0
    assert not backend.status().data.get("update_available")


def test_after_updating_the_old_answer_no_longer_counts(tmp_path):
    adapter = FakeAdapter(report=AVAILABLE)
    backend = _backend(tmp_path, adapter)
    backend.check_update()
    adapter.version = "2.93.0"  # the player pressed Update
    assert not backend.status().data.get("update_available")


def test_a_failed_check_does_not_raise_or_claim_an_update(tmp_path):
    backend = _backend(tmp_path, FakeAdapter(error=True))
    backend.check_update()
    assert not backend.status().data.get("update_available")


def test_an_unreadable_answer_is_not_cached_as_up_to_date(tmp_path):
    _backend(tmp_path, FakeAdapter(report=AVAILABLE)).check_update()
    garbled = FakeAdapter(report={"installed": None, "newest": None, "available": None})
    backend = _backend(tmp_path, garbled)
    backend._update_check_path().unlink()
    backend.check_update()
    assert not backend._update_check_path().exists()


def test_the_check_is_not_repeated_within_the_interval(tmp_path):
    adapter = FakeAdapter(report=UP_TO_DATE)
    backend = _backend(tmp_path, adapter)
    backend.check_update()
    backend.check_update()
    assert adapter.checks == 1


def test_a_stale_answer_is_checked_again(tmp_path, monkeypatch):
    import nadare.backends.fgo.backend as mod

    adapter = FakeAdapter(report=UP_TO_DATE)
    backend = _backend(tmp_path, adapter)
    clock = [1000.0]
    monkeypatch.setattr(mod.time, "time", lambda: clock[0])
    backend.check_update()
    clock[0] += mod.UPDATE_CHECK_INTERVAL + 1
    backend.check_update()
    assert adapter.checks == 2


def test_update_checks_off_in_settings_skips_the_check(tmp_path):
    adapter = FakeAdapter(report=AVAILABLE)
    backend = _backend(tmp_path, adapter, check_updates=False)
    backend.check_update()
    assert adapter.checks == 0


def test_not_installed_skips_the_check(tmp_path):
    adapter = FakeAdapter(report=AVAILABLE)
    adapter.installed = False
    backend = _backend(tmp_path, adapter)
    backend.check_update()
    assert adapter.checks == 0


def test_the_page_schedules_the_check_for_fgo(tmp_path):
    from types import SimpleNamespace

    from nadare.core.config import Config
    from nadare.gui.main_window import MainWindow

    cfg = Config.load(tmp_path / "config.json")
    cfg.add_profile("fgo", backend="fgo-lepton")
    assert MainWindow._has_direct_manifest(SimpleNamespace(config=cfg), "fgo") is True
