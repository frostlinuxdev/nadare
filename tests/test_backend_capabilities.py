# SPDX-License-Identifier: GPL-3.0-or-later
"""Optional backend operations must match the calls made by the GUI."""

from __future__ import annotations

import inspect

import pytest
from _backend_helpers import make_context, write_exe

from nadare.core import backends
from nadare.gui.game_view_parts.busy import BusyMixin, can_pause_kind
from nadare.gui.workers import PAUSABLE, BackendWorker


class MinimalBackend(backends.GameBackend):
    kind = "contract-minimal"

    def status(self):
        return backends.Status("installed")

    def install(self):
        pass

    def update(self):
        pass

    def launch(self):
        return 0

    def stop(self):
        pass


@pytest.fixture
def register(monkeypatch):
    def add(cls):
        monkeypatch.setitem(backends._REGISTRY, cls.kind, cls)
        return cls

    return add


def test_declared_remember_signature_matches_the_worker():
    inspect.signature(backends.GameBackend.set_remember).bind(
        object(), True, persist_flag=False
    )


def test_optional_predownload_protocol_matches_the_worker():
    inspect.signature(backends.PredownloadBackend.predownload).bind(object())


def test_minimal_declared_backend_runs_through_worker(tmp_path, register):
    register(MinimalBackend)
    ctx = make_context(tmp_path, MinimalBackend.kind)
    worker = BackendWorker(ctx.config, ctx.name, "update")
    succeeded, failed = [], []
    worker.succeeded.connect(succeeded.append)
    worker.failed.connect(failed.append)
    worker.run()
    assert succeeded == [0]
    assert failed == []


def test_remember_worker_leaves_persistence_to_caller(tmp_path, register):
    calls = []

    @register
    class RememberBackend(MinimalBackend):
        kind = "contract-remember"
        supports_remember_me = True

        def set_remember(self, enabled: bool, persist_flag: bool = True) -> None:
            calls.append((enabled, persist_flag))

    ctx = make_context(tmp_path, RememberBackend.kind, {"remember_me": False})
    worker = BackendWorker(ctx.config, ctx.name, "set_remember", payload=True)
    assert worker._dispatch(RememberBackend(ctx)) == 0
    assert calls == [(True, False)]
    assert ctx.profile["remember_me"] is False


def test_new_resumable_kind_needs_no_gui_kind_list(register):
    @register
    class ResumableBackend(MinimalBackend):
        kind = "contract-new-resumable"
        supports_resume = True

    assert can_pause_kind(ResumableBackend.kind)
    view = type("View", (), {"_profile_data": {"backend": ResumableBackend.kind}})()
    for operation in PAUSABLE:
        view._busy_op = operation
        assert BusyMixin._can_pause(view)
        inspect.signature(getattr(ResumableBackend, operation)).bind(object())
    view._busy_op = "launch"
    assert not BusyMixin._can_pause(view)


@pytest.mark.parametrize("kind", [MinimalBackend.kind, "unknown-contract-kind", ""])
def test_unsupported_and_unknown_kinds_are_not_pausable(kind, register):
    register(MinimalBackend)
    assert not can_pause_kind(kind)


@pytest.mark.parametrize("advertised", [False, True])
def test_worker_predownload_obeys_declaration(tmp_path, advertised):
    calls = []

    class PredownloadBackend(MinimalBackend):
        supports_predownload = advertised

        def predownload(self) -> dict:
            calls.append("predownload")
            return {}

    ctx = make_context(tmp_path, MinimalBackend.kind)
    worker = BackendWorker(ctx.config, ctx.name, "predownload")
    if advertised:
        assert worker._dispatch(PredownloadBackend(ctx)) == 0
        assert calls == ["predownload"]
    else:
        with pytest.raises(backends.BackendUnsupported, match="no predownload"):
            worker._dispatch(PredownloadBackend(ctx))
        assert calls == []


def test_minimal_backend_rejects_optional_worker_calls(tmp_path):
    ctx = make_context(tmp_path, MinimalBackend.kind)
    backend = MinimalBackend(ctx)
    for operation in ("predownload", "set_remember", "login", "logout", "remake_prefix"):
        worker = BackendWorker(ctx.config, ctx.name, operation)
        with pytest.raises(backends.BackendUnsupported):
            worker._dispatch(backend)
    assert not hasattr(backend, "predownload")


@pytest.mark.parametrize("kind", backends.kinds())
def test_real_backend_capabilities_match_implementations(kind, tmp_path):
    from nadare.backends.direct import DirectBackend
    from nadare.core.events import Cancelled

    cls = backends.get(kind)
    ctx = make_context(tmp_path, kind)
    backend = cls(ctx)
    # A shared inherited predownload hook without a source is unsupported.
    has_source = callable(getattr(cls, "predownload_factory", None))
    assert cls.supports_predownload == has_source
    if cls.supports_predownload:
        inspect.signature(cls.predownload).bind(backend)
        # Every real implementation honours cancellation before filesystem/network work.
        ctx.cancel.cancel()
        with pytest.raises(Cancelled):
            backend.predownload()
    elif hasattr(backend, "predownload"):
        with pytest.raises(backends.BackendError, match="no predownload"):
            backend.predownload()

    assert cls.supports_resume == issubclass(cls, DirectBackend)
    if cls.supports_resume:
        for operation in PAUSABLE:
            inspect.signature(getattr(cls, operation)).bind(backend)
    assert cls.supports_remake_prefix == (
        cls.remake_prefix is not backends.GameBackend.remake_prefix
    )
    assert backends.supports_remake_prefix(kind) == cls.supports_remake_prefix
    if cls.supports_remake_prefix:
        inspect.signature(cls.remake_prefix).bind(backend)
    assert cls.supports_remember_me == (
        cls.set_remember is not backends.GameBackend.set_remember
    )
    if cls.supports_remember_me:
        inspect.signature(cls.set_remember).bind(backend, True, persist_flag=False)
        inspect.signature(cls.logout).bind(backend)
        assert cls.set_remember is not backends.GameBackend.set_remember
    if cls.supports_login:
        inspect.signature(cls.login).bind(backend)
    if cls.supports_update:
        inspect.signature(cls.update).bind(backend)


@pytest.mark.parametrize(
    "kind",
    [
        kind for kind in backends.kinds()
        if callable(getattr(backends.get(kind), "predownload_factory", None))
    ],
)
def test_every_real_predownload_source_can_complete_offline(kind, tmp_path, monkeypatch):
    from nadare.core import directdl, sophon

    cls = backends.get(kind)
    assert cls.supports_predownload
    ctx = make_context(tmp_path, kind, {"prefix": str(tmp_path / "prefix")})
    backend = cls(ctx)
    write_exe(backend.direct_target(), "MZ")
    backend._write_version_file("1.0", backend.game_tag)
    package = sophon.SophonPackage("next", "https://invalid/m", "https://invalid/c", "m2", 1)
    plan = directdl.Plan(backend.game_tag, "2.0", sophon=(package,))
    monkeypatch.setattr(cls, "predownload_factory", staticmethod(lambda *a, **kw: plan))
    calls = []

    def download(plan, dest, cache, *, emit, cancel, workers):
        calls.append((plan, dest, cache, cancel))
        return {"bytes": 123}

    monkeypatch.setattr(directdl, "predownload", download)
    assert backend.predownload() == {"bytes": 123}
    assert calls == [(plan, backend.install_dir(), backend._predownload_cache("2.0"), ctx.cancel)]
    state = backend._read_predownload_state()
    assert state["version"] == "2.0" and state["ready"] is True


@pytest.mark.parametrize(
    "kind", [kind for kind in backends.kinds() if backends.get(kind).supports_resume]
)
@pytest.mark.parametrize("operation", sorted(PAUSABLE))
def test_real_resumable_operations_keep_partial_downloads(
    kind, operation, tmp_path, monkeypatch
):
    from nadare.core import directdl
    from nadare.core.events import Cancel, Cancelled

    ctx = make_context(
        tmp_path, kind,
        {"prefix": str(tmp_path / "prefix"), "manifest_file": "offline-manifest.json"},
    )
    backend = backends.get(kind)(ctx)
    write_exe(backend.direct_target(), "MZ")
    # Strinova checks the tree against the source's row list; an empty list keeps it neutral.
    plan = directdl.Plan(
        backend.game_tag, "2.0", exe=backend.direct_exe, extra={"verification_files": ()}
    )
    monkeypatch.setattr(backend, "plan", lambda: plan)
    monkeypatch.setattr(backend, "_ensure_default_runner", lambda **kw: None)
    monkeypatch.setattr(backend, "_ensure_direct_prefix", lambda: None)
    monkeypatch.setattr(backend, "_refuse_if_running", lambda *a: None)
    if hasattr(backend, "_ensure_webview2"):
        monkeypatch.setattr(backend, "_ensure_webview2", lambda: None)
    partial = backend.install_dir() / "download.part"
    calls = []

    def sync(plan, dest, *, emit, cancel, workers, changed):
        calls.append(cancel)
        if len(calls) == 1:
            partial.write_bytes(b"partial")
            cancel.cancel()
            cancel.raise_if_cancelled()
        assert partial.read_bytes() == b"partial"
        cancel.raise_if_cancelled()
        return {"downloaded": 1}

    monkeypatch.setattr(directdl, "sync", sync)
    with pytest.raises(Cancelled):
        getattr(backend, operation)()
    assert partial.read_bytes() == b"partial"
    ctx.cancel = Cancel()  # A resumed worker creates a fresh cancellation token.
    getattr(backend, operation)()
    assert len(calls) == 2
    assert backend._read_version_file()["version"] == "2.0"
