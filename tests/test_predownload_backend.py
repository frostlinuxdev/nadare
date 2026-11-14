# SPDX-License-Identifier: GPL-3.0-or-later
"""Predownload in the direct backends, the locks and the CLI (HoYo games first).

``check_update`` notices an open predownload window; ``predownload()`` fetches the next version
into ``<game>/.nadare/predownload/<version>/`` while the game stays playable; the update that
follows uses that cache and removes it. All offline: plans and syncs are fakes.
"""

from __future__ import annotations

import json

import pytest
from _backend_helpers import make_context, write_exe

from nadare import app
from nadare.backends import direct
from nadare.core import directdl as dd
from nadare.core import process, sophon
from nadare.core.backends import BackendError


def _pkg(version: str) -> sophon.SophonPackage:
    return sophon.SophonPackage(f"zzz-{version}", "https://x/m", "https://x/c", f"m{version}", 1)


def _plan(version: str) -> dd.Plan:
    return dd.Plan("zzz", version, exe="ZenlessZoneZero.exe", sophon=(_pkg(version),))


class Calls:
    def __init__(self):
        self.predownload: list[tuple] = []
        self.sync: list[dict] = []


@pytest.fixture
def calls(monkeypatch):
    seen = Calls()

    def fake_predownload(plan, dest, cache, *, emit=None, cancel=None, workers=4):
        seen.predownload.append((plan.version, dest, cache))
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "chunk").write_bytes(b"c")
        (cache / f"{plan.sophon[0].name}.ready").write_text(plan.sophon[0].manifest_id)
        return {"bytes": 1234, "packages": 1}

    def fake_sync(plan, dest, *, emit=None, cancel=None, workers=4, changed=None, **kwargs):
        seen.sync.append({"version": plan.version, **kwargs})
        return {"downloaded": 1, "skipped": 0}

    monkeypatch.setattr(dd, "predownload", fake_predownload)
    monkeypatch.setattr(dd, "sync", fake_sync)
    return seen


def _zzz(tmp_path, monkeypatch, *, installed="3.3.0", main="3.3.0", pre="3.4.0", profile=None):
    monkeypatch.setattr(
        direct.ZenlessBackend, "plan_factory", staticmethod(lambda fetch=None: _plan(main))
    )
    monkeypatch.setattr(
        direct.ZenlessBackend,
        "predownload_factory",
        staticmethod(lambda fetch=None: _plan(pre) if pre else None),
    )
    backend = direct.ZenlessBackend(
        make_context(tmp_path, "direct-zzz", {"prefix": str(tmp_path / "pfx"), **(profile or {})})
    )
    if installed:
        write_exe(backend.direct_target(), "MZ")
        backend._write_version_file(installed, "zzz")
        backend._write_latest(installed)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda strict=False: None, raising=False)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: None, raising=False)
    return backend


def _age_state(backend) -> None:
    """Make the last predownload check look old, so the next ``check_update`` asks again."""
    path = backend.install_dir() / ".nadare" / "predownload.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["checked_at"] = 0
    path.write_text(json.dumps(state), encoding="utf-8")


# --------------------------------------------------------------- which games offer it
def test_games_with_a_predownload_source_offer_predownload():
    for cls in (
        direct.GenshinBackend,
        direct.StarRailBackend,
        direct.ZenlessBackend,
        direct.PgrBackend,
        direct.WutheringWavesBackend,
        direct.Hi3Backend,
    ):
        assert callable(cls.predownload_factory)
    assert direct.BrownDust2Backend.predownload_factory is None


# --------------------------------------------------------------- check_update + status
def test_an_open_window_shows_in_the_status(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    backend.check_update()
    data = backend.status().data
    assert data["predownload_version"] == "3.4.0"
    assert data["predownload_ready"] is False


def test_no_window_means_no_predownload_in_the_status(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, pre=None)
    backend.check_update()
    assert "predownload_version" not in backend.status().data


def test_a_closed_window_clears_the_old_notice(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    backend.check_update()
    assert backend.status().data.get("predownload_version") == "3.4.0"
    monkeypatch.setattr(
        direct.ZenlessBackend, "predownload_factory", staticmethod(lambda fetch=None: None)
    )
    _age_state(backend)  # the next check really asks
    backend.check_update()
    assert "predownload_version" not in backend.status().data


def test_a_failing_check_keeps_what_was_known_and_never_raises(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    backend.check_update()

    def boom(fetch=None):
        raise dd.DirectError("offline")

    monkeypatch.setattr(direct.ZenlessBackend, "predownload_factory", staticmethod(boom))
    _age_state(backend)
    backend.check_update()  # must not raise
    assert backend.status().data.get("predownload_version") == "3.4.0"


def test_update_checks_switched_off_never_ask(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(
        direct.ZenlessBackend,
        "predownload_factory",
        staticmethod(lambda fetch=None: asked.append(1) or _plan("3.4.0")),
    )
    backend.ctx.config.data["check_updates"] = False
    backend.check_update()
    assert asked == []


def test_the_check_is_not_repeated_within_the_interval(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    asked = []
    monkeypatch.setattr(
        direct.ZenlessBackend,
        "predownload_factory",
        staticmethod(lambda fetch=None: asked.append(1) or _plan("3.4.0")),
    )
    backend.check_update()
    backend.check_update()
    assert len(asked) == 1


def test_not_installed_never_asks(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, installed=None)
    asked = []
    monkeypatch.setattr(
        direct.ZenlessBackend,
        "predownload_factory",
        staticmethod(lambda fetch=None: asked.append(1) or _plan("3.4.0")),
    )
    backend.check_update()
    assert asked == []
    assert "predownload_version" not in backend.status().data


def test_once_the_version_is_live_the_page_offers_update_not_predownload(
    tmp_path, monkeypatch, calls
):
    backend = _zzz(tmp_path, monkeypatch)
    backend.check_update()
    backend._write_latest("3.4.0")  # release day: the published version is the predownloaded one
    data = backend.status().data
    assert data["update_available"] and "predownload_version" not in data


def test_a_window_for_the_installed_version_is_not_shown(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, pre="3.3.0")
    backend.check_update()
    assert "predownload_version" not in backend.status().data


# --------------------------------------------------------------- predownload()
def test_predownload_fetches_into_the_versioned_cache_and_marks_ready(
    tmp_path, monkeypatch, calls
):
    backend = _zzz(tmp_path, monkeypatch)
    backend.predownload()
    (version, dest, cache) = calls.predownload[0]
    assert version == "3.4.0" and dest == backend.install_dir()
    assert cache == backend.install_dir() / ".nadare" / "predownload" / "3.4.0"
    data = backend.status().data
    assert data["predownload_version"] == "3.4.0" and data["predownload_ready"] is True
    assert calls.sync == []  # nothing in the game was synced


def test_predownload_runs_while_the_game_is_open(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)

    def refuse(*_a, **_k):
        raise BackendError("game is running")

    monkeypatch.setattr(backend, "_refuse_if_running", refuse)
    backend.predownload()
    assert calls.predownload


def test_predownload_needs_an_installed_game(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, installed=None)
    with pytest.raises(BackendError, match="not installed"):
        backend.predownload()
    assert calls.predownload == []


def test_predownload_without_a_window_says_so(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, pre=None)
    with pytest.raises(BackendError, match="(?i)no predownload"):
        backend.predownload()


def test_predownload_of_the_installed_version_does_nothing(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, pre="3.3.0")
    with pytest.raises(BackendError, match="(?i)no predownload"):
        backend.predownload()
    assert calls.predownload == []


def test_a_game_without_predownload_refuses(tmp_path, monkeypatch, calls):
    backend = direct.BrownDust2Backend(make_context(tmp_path, "direct-bd2", {}))
    write_exe(backend.direct_target(), "MZ")
    with pytest.raises(BackendError, match="(?i)predownload"):
        backend.predownload()


def test_a_publisher_error_becomes_a_backend_error(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise dd.DirectError("bad manifest")

    monkeypatch.setattr(dd, "predownload", boom)
    with pytest.raises(BackendError, match="bad manifest"):
        backend.predownload()
    assert backend.status().data.get("predownload_ready") in (None, False)


def test_predownload_removes_caches_of_other_versions(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    old = backend.install_dir() / ".nadare" / "predownload" / "3.2.0"
    old.mkdir(parents=True)
    (old / "chunk").write_bytes(b"x")
    backend.predownload()
    assert not old.exists()
    assert (backend.install_dir() / ".nadare" / "predownload" / "3.4.0").is_dir()


# --------------------------------------------------------------- the update uses the cache
def test_the_update_uses_the_cache_then_removes_it(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    backend.predownload()
    cache = backend.install_dir() / ".nadare" / "predownload" / "3.4.0"
    monkeypatch.setattr(
        direct.ZenlessBackend, "plan_factory", staticmethod(lambda fetch=None: _plan("3.4.0"))
    )
    backend.update()
    assert calls.sync[-1] == {"version": "3.4.0", "chunk_cache": cache}
    assert not (backend.install_dir() / ".nadare" / "predownload").exists()
    assert "predownload_version" not in backend.status().data


def test_an_update_without_a_cache_passes_none(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, main="3.4.0")
    backend.update()
    assert calls.sync[-1] == {"version": "3.4.0"}  # old sync signature still works


def test_a_cache_for_another_version_is_not_used(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, main="3.3.1")
    other = backend.install_dir() / ".nadare" / "predownload" / "3.4.0"
    other.mkdir(parents=True)
    backend.update()
    assert calls.sync[-1] == {"version": "3.3.1"}
    assert other.is_dir()  # still useful for 3.4.0


def test_a_failed_update_keeps_the_cache(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    backend.predownload()
    monkeypatch.setattr(
        direct.ZenlessBackend, "plan_factory", staticmethod(lambda fetch=None: _plan("3.4.0"))
    )

    def failing(plan, dest, **kwargs):
        raise dd.DirectError("network down")

    monkeypatch.setattr(dd, "sync", failing)
    with pytest.raises(BackendError):
        backend.update()
    assert (backend.install_dir() / ".nadare" / "predownload" / "3.4.0").is_dir()


def test_cleanup_never_follows_a_planted_link(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, main="3.4.0")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    state = backend.install_dir() / ".nadare"
    state.mkdir(parents=True, exist_ok=True)
    (state / "predownload").symlink_to(outside, target_is_directory=True)
    backend.update()
    assert (outside / "precious.txt").read_text() == "keep me"
    assert calls.sync[-1] == {"version": "3.4.0"}  # a linked cache is never read either


def test_the_state_file_is_plain_json(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    backend.predownload()
    state = json.loads(
        (backend.install_dir() / ".nadare" / "predownload.json").read_text(encoding="utf-8")
    )
    assert state["version"] == "3.4.0" and state["ready"] is True


# --------------------------------------------------------------- locks
def test_a_predownload_does_not_block_launch_but_other_operations_do(tmp_path):
    ops = process.OperationLock("zzz")
    predl = process.PredownloadLock("zzz")
    ops.acquire()
    try:
        assert process.launch_blocked("zzz")  # an install/update/repair
        predl.acquire()
        try:
            assert not process.launch_blocked("zzz")  # only a predownload
        finally:
            predl.release()
    finally:
        ops.release()
    assert not process.launch_blocked("zzz")


def test_the_cli_predownload_holds_both_locks():
    assert "predownload" in app.COMMANDS
    assert "predownload" in app._LOCKED_COMMANDS
    with app._operation_lock("predownload", "zzz"):
        assert process.operation_in_progress("zzz")
        assert process.predownload_in_progress("zzz")
        assert not process.launch_blocked("zzz")
    assert not process.operation_in_progress("zzz")
    assert not process.predownload_in_progress("zzz")


def test_the_cli_launch_is_allowed_during_a_predownload():
    with app._operation_lock("predownload", "zzz"):
        with app._operation_lock("launch", "zzz"):
            pass


def test_the_cli_launch_is_refused_during_an_update():
    with app._operation_lock("update", "zzz"):
        with pytest.raises(BackendError):
            with app._operation_lock("launch", "zzz"):
                pass


def test_an_update_is_refused_during_a_predownload():
    with app._operation_lock("predownload", "zzz"):
        with pytest.raises(BackendError):
            with app._operation_lock("update", "zzz"):
                pass


def test_the_cli_has_a_predownload_command():
    args = app.build_parser().parse_args(["predownload", "-p", "zzz"])
    assert args.command == "predownload" and args.profile == "zzz"


@pytest.mark.parametrize("version", ["..", ".", "a/b", ""])
def test_an_unsafe_version_never_becomes_a_cache_folder(tmp_path, monkeypatch, calls, version):
    backend = _zzz(tmp_path, monkeypatch, pre=version or None)
    if version:
        with pytest.raises(BackendError):
            backend.predownload()
        assert calls.predownload == []
    root = backend.install_dir() / ".nadare" / "predownload"
    assert not root.exists() or all(p.parent == root for p in root.iterdir())


def test_a_link_planted_at_the_version_folder_is_replaced_not_followed(
    tmp_path, monkeypatch, calls
):
    backend = _zzz(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    root = backend.install_dir() / ".nadare" / "predownload"
    root.mkdir(parents=True)
    (root / "3.4.0").symlink_to(outside, target_is_directory=True)
    backend.predownload()
    assert list(outside.iterdir()) == []
    assert not (root / "3.4.0").is_symlink() and (root / "3.4.0").is_dir()


def test_a_linked_version_folder_is_never_used_by_the_update(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch, main="3.4.0")
    outside = tmp_path / "outside"
    outside.mkdir()
    root = backend.install_dir() / ".nadare" / "predownload"
    root.mkdir(parents=True)
    (root / "3.4.0").symlink_to(outside, target_is_directory=True)
    backend.update()
    assert calls.sync[-1] == {"version": "3.4.0"}


def test_clearing_a_linked_cache_root_removes_only_the_link(tmp_path, monkeypatch, calls):
    backend = _zzz(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("keep me")
    state = backend.install_dir() / ".nadare"
    state.mkdir(parents=True, exist_ok=True)
    (state / "predownload").symlink_to(outside, target_is_directory=True)
    backend._clear_predownload_cache()
    assert not (state / "predownload").is_symlink()
    assert (outside / "precious.txt").read_text() == "keep me"
