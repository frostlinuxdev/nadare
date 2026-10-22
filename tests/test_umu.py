# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed umu-launcher: download/verify policy and backend integration.

No test here touches the network: the downloader and the version smoke test are
replaced with in-memory fakes, exactly like ``tests/test_download.py``.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends.czn.backend import CznBackend
from nadare.backends.wine import WineBackend
from nadare.core import download as dl
from nadare.core import hosting, umu
from nadare.core.backends import BackendError

URL = (
    "https://github.com/Open-Wine-Components/umu-launcher/releases/download/"
    "1.4.4/umu-launcher-1.4.4-zipapp.tar"
)


def _tar_bytes(
    members: dict[str, bytes],
    *,
    symlinks: tuple[tuple[str, str], ...] = (),
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
        for name, target in symlinks:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    return buf.getvalue()


class _Completed:
    returncode = 0


def _fake_download(monkeypatch, payload: bytes, calls: list | None = None):
    def fake(url, dest, **kwargs):
        if calls is not None:
            calls.append((url, Path(dest), kwargs))
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        return dl.DownloadResult(
            dest,
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            hashlib.sha512(payload).hexdigest(),
        )

    monkeypatch.setattr(umu.download, "download", fake)


def _fake_smoke(monkeypatch, code: int = 0) -> list:
    calls: list = []

    def run(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return _Completed() if code == 0 else type("R", (), {"returncode": code})()

    monkeypatch.setattr(umu.subprocess, "run", run)
    return calls


def _only_python(monkeypatch) -> None:
    monkeypatch.setattr(
        umu.shutil, "which", lambda name, *a, **k: "/usr/bin/python3" if name == "python3" else None
    )


def _payload() -> bytes:
    return _tar_bytes({"umu/umu-run": b"#!/usr/bin/python3\nprint('umu')\n"})


# ---------------------------------------------------------------------------
# core/umu.py
# ---------------------------------------------------------------------------
def test_constants_and_url() -> None:
    assert umu.UMU_VERSION == "1.4.4"
    assert len(umu.UMU_TAR_SHA256) == 64
    assert hosting.umu_zipapp_url() == URL


def test_managed_path_is_versioned_under_data_dir() -> None:
    assert umu.managed_path() == Path(
        umu.paths.data_dir() / "umu" / "1.4.4" / "umu-run"
    )


def test_managed_requires_an_executable_file_and_python3(monkeypatch) -> None:
    dest = umu.managed_path()
    dest.parent.mkdir(parents=True)
    dest.write_text("#!/usr/bin/python3\n")
    dest.chmod(0o755)
    _only_python(monkeypatch)
    assert umu.managed() == dest

    dest.chmod(0o644)
    assert umu.managed() is None
    dest.chmod(0o755)
    assert umu.managed() == dest

    monkeypatch.setattr(umu.shutil, "which", lambda name, *a, **k: None)
    assert umu.managed() is None


def test_ensure_extracts_only_umu_run_and_ignores_extras(monkeypatch) -> None:
    payload = _tar_bytes(
        {
            "umu/umu-run": b"#!/usr/bin/python3\nprint('umu')\n",
            "umu/README.md": b"docs",
        },
        symlinks=(("umu/link", "umu-run"),),
    )
    calls: list = []
    _fake_download(monkeypatch, payload, calls)
    smoke = _fake_smoke(monkeypatch)
    _only_python(monkeypatch)

    dest = umu.ensure_managed()

    assert dest == umu.managed_path()
    assert dest.is_file()
    assert dest.read_bytes() == b"#!/usr/bin/python3\nprint('umu')\n"
    assert dest.stat().st_mode & 0o111
    assert sorted(p.name for p in dest.parent.iterdir()) == ["umu-run"]
    assert len(calls) == 1
    assert calls[0][0] == URL
    assert calls[0][2]["expected_sha256"] == umu.UMU_TAR_SHA256
    assert smoke and smoke[0][0][0] == "/usr/bin/python3"
    assert smoke[0][0][1] == str(dest)


def test_ensure_rejects_a_symlinked_umu_run(monkeypatch) -> None:
    payload = _tar_bytes({}, symlinks=(("umu/umu-run", "/etc/passwd"),))
    _fake_download(monkeypatch, payload)
    _fake_smoke(monkeypatch)
    _only_python(monkeypatch)

    with pytest.raises(umu.UmuError):
        umu.ensure_managed()
    assert not umu.managed_path().exists()


def test_ensure_rejects_a_missing_umu_run(monkeypatch) -> None:
    _fake_download(monkeypatch, _tar_bytes({"umu/other": b"x"}))
    _fake_smoke(monkeypatch)
    _only_python(monkeypatch)

    with pytest.raises(umu.UmuError):
        umu.ensure_managed()
    assert not umu.managed_path().exists()


def test_ensure_hash_mismatch_raises_and_leaves_nothing_behind(monkeypatch) -> None:
    def bad_download(*_a, **_k):
        raise dl.IntegrityError("sha256 mismatch")

    monkeypatch.setattr(umu.download, "download", bad_download)
    _fake_smoke(monkeypatch)
    _only_python(monkeypatch)

    with pytest.raises(umu.UmuError):
        umu.ensure_managed()
    assert not umu.managed_path().exists()


def test_ensure_failing_smoke_test_removes_the_file(monkeypatch) -> None:
    _fake_download(monkeypatch, _payload())
    _fake_smoke(monkeypatch, code=1)
    _only_python(monkeypatch)

    with pytest.raises(umu.UmuError):
        umu.ensure_managed()
    assert not umu.managed_path().exists()


def test_ensure_requires_python3_without_downloading(monkeypatch) -> None:
    monkeypatch.setattr(umu.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(umu.download, "download", lambda *a, **k: pytest.fail("downloaded"))

    with pytest.raises(umu.UmuError, match="python3"):
        umu.ensure_managed()


def test_second_ensure_is_a_no_op(monkeypatch) -> None:
    _fake_download(monkeypatch, _payload())
    _fake_smoke(monkeypatch)
    _only_python(monkeypatch)
    dest = umu.ensure_managed()

    monkeypatch.setattr(umu.download, "download", lambda *a, **k: pytest.fail("downloaded again"))
    assert umu.ensure_managed() == dest


def test_resolve_prefers_system_then_managed_then_download(monkeypatch) -> None:
    monkeypatch.setattr(
        umu.shutil, "which", lambda name, *a, **k: "/usr/bin/umu-run" if name == "umu-run" else None
    )
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))
    assert umu.resolve(allow_download=True) == "/usr/bin/umu-run"

    dest = umu.managed_path()
    dest.parent.mkdir(parents=True)
    dest.write_text("#!/usr/bin/python3\n")
    dest.chmod(0o755)
    _only_python(monkeypatch)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))
    assert umu.resolve(allow_download=False) == str(dest)


def test_resolve_does_not_download_when_not_allowed(monkeypatch) -> None:
    monkeypatch.setattr(umu.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))
    assert umu.resolve(allow_download=False) is None


def test_resolve_falls_back_cleanly_when_setup_fails(monkeypatch) -> None:
    monkeypatch.setattr(umu.shutil, "which", lambda name, *a, **k: None)

    def boom(**_k):
        raise umu.UmuError("offline")

    monkeypatch.setattr(umu, "ensure_managed", boom)
    assert umu.resolve(allow_download=True) is None


# ---------------------------------------------------------------------------
# policy matrix through the Arknights command builder
# ---------------------------------------------------------------------------
def _ak_backend(tmp_path, monkeypatch, missing):
    prefix = tmp_path / "prefix"
    exe = write_exe(prefix / ak.DEFAULT_INSTALL_SUBDIR / "game launcher.exe", "MZ")
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    monkeypatch.setattr(ak.runners, "resolve_runner", lambda value: proton)
    monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(ak.hostlibs, "missing_32bit", lambda: missing)
    backend = ak.ArknightsBackend(
        make_context(tmp_path, ak.KIND, {"prefix": str(prefix), "runner": "MyProton"})
    )
    return backend, proton, exe


def test_system_umu_is_used_without_downloading(tmp_path, monkeypatch) -> None:
    backend, _proton, exe = _ak_backend(tmp_path, monkeypatch, ["libfreetype.so.6"])
    monkeypatch.setattr(
        ak.shutil, "which", lambda name, *a, **k: "/opt/umu-run" if name == "umu-run" else None
    )
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))

    argv, _env, _target = backend.build_launch()
    assert argv == ["/opt/umu-run", str(exe)]


def test_working_32bit_libs_run_proton_directly_without_downloading(tmp_path, monkeypatch) -> None:
    backend, proton, exe = _ak_backend(tmp_path, monkeypatch, [])
    monkeypatch.setattr(ak.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))

    argv, _env, _target = backend.build_launch()
    assert argv[:2] == [str(proton / "proton"), "run"]
    assert argv[-1] == str(exe)


def test_missing_libs_and_no_system_umu_downloads_once_and_uses_managed(
    tmp_path, monkeypatch
) -> None:
    backend, _proton, exe = _ak_backend(tmp_path, monkeypatch, ["libfreetype.so.6"])
    calls: list = []
    _fake_download(monkeypatch, _payload(), calls)
    _fake_smoke(monkeypatch)
    monkeypatch.setattr(
        ak.shutil,
        "which",
        lambda name, *a, **k: "/usr/bin/python3" if name == "python3" else None,
    )

    argv, _env, _target = backend.build_launch()
    assert argv == [str(umu.managed_path()), str(exe)]
    assert len(calls) == 1


def test_download_failure_falls_back_to_the_host_libs_hint(tmp_path, monkeypatch) -> None:
    backend, _proton, _exe = _ak_backend(tmp_path, monkeypatch, ["libfreetype.so.6"])

    def offline(*_a, **_k):
        raise dl.DownloadError("offline")

    monkeypatch.setattr(umu.download, "download", offline)
    _fake_smoke(monkeypatch)
    monkeypatch.setattr(
        ak.shutil,
        "which",
        lambda name, *a, **k: "/usr/bin/python3" if name == "python3" else None,
    )

    with pytest.raises(BackendError) as excinfo:
        backend.build_launch()
    assert "libfreetype.so.6" in str(excinfo.value)


def test_flatpak_behaviour_is_unchanged(tmp_path, monkeypatch) -> None:
    backend, proton, exe = _ak_backend(tmp_path, monkeypatch, ["libfreetype.so.6"])
    monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(ak.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))

    argv, _env, _target = backend.build_launch()
    assert argv == [str(proton / "proton"), "run", str(exe)]


# ---------------------------------------------------------------------------
# status paths must never download
# ---------------------------------------------------------------------------
def _wine_backend(tmp_path, proton):
    game = tmp_path / "game"
    write_exe(game / "game.exe", "MZ")
    return WineBackend(
        make_context(
            tmp_path,
            "wine",
            {
                "install_dir": str(game),
                "exe_name": "game.exe",
                "prefix": str(tmp_path / "prefix"),
                "proton_runner": str(proton),
            },
        )
    )


def test_wine_status_accepts_a_managed_copy_without_downloading(tmp_path, monkeypatch) -> None:
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    backend = _wine_backend(tmp_path, proton)
    monkeypatch.setattr(backend, "_runner_path", lambda runner: proton)
    monkeypatch.setattr(ak.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(ak.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))

    assert backend._runtime_error() is None


def test_wine_status_does_not_require_umu_when_managed_setup_is_possible(
    tmp_path, monkeypatch
) -> None:
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    backend = _wine_backend(tmp_path, proton)
    monkeypatch.setattr(backend, "_runner_path", lambda runner: proton)
    monkeypatch.setattr(ak.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(ak.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))

    assert WineBackend.status(backend).state != "error"


# ---------------------------------------------------------------------------
# other builders reuse the same resolver
# ---------------------------------------------------------------------------
def test_wine_launch_uses_a_downloaded_managed_umu(tmp_path, monkeypatch) -> None:
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    backend = _wine_backend(tmp_path, proton)
    monkeypatch.setattr(backend, "_runner_path", lambda runner: proton)
    missing = tmp_path / "managed-umu"
    write_exe(missing, "#!/usr/bin/python3\n")
    calls: list = []

    def ensure(**_k):
        calls.append(1)
        return missing

    monkeypatch.setattr(ak.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(ak.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
    monkeypatch.setattr(umu, "ensure_managed", ensure)

    argv, _env, _prefix, target = backend.build()
    assert argv == [str(missing), str(target)]
    assert calls == [1]


def test_czn_launch_uses_a_downloaded_managed_umu(tmp_path, monkeypatch) -> None:
    prefix = tmp_path / "prefix"
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("#!/bin/sh\n")
    backend = CznBackend(make_context(tmp_path, "stove-czn", {"prefix": str(prefix)}))
    backend.ctx.profile["runner"] = str(proton)
    monkeypatch.setattr(backend, "resolve_runner", lambda: (str(proton), "proton"))
    missing = tmp_path / "managed-umu"
    write_exe(missing, "#!/usr/bin/python3\n")
    calls: list = []

    def ensure(**_k):
        calls.append(1)
        return missing

    monkeypatch.setattr(ak.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(ak.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(ak.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
    monkeypatch.setattr(umu, "ensure_managed", ensure)

    argv, _env = backend._prefix_command(["game.exe"])
    assert argv == [str(missing), "game.exe"]
    assert calls == [1]
