# SPDX-License-Identifier: GPL-3.0-or-later
"""Managed umu-launcher: download/verify policy and backend integration.

No test here touches the network: the downloader and the version smoke test are
replaced with in-memory fakes, exactly like ``tests/test_download.py``.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from _backend_helpers import make_context, write_exe

from nadare.backends import arknights as ak
from nadare.backends import winegame as wg
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


def _zipapp_payload() -> bytes:
    """A minimal executable zipapp, like the real umu-run (runs with any Python 3)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("__main__.py", "raise SystemExit(0)\n")
    return _tar_bytes({"umu/umu-run": buffer.getvalue()})


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


def test_managed_needs_an_executable_file_and_falls_back_without_python3(
    monkeypatch, tmp_path
) -> None:
    dest = umu.managed_path()
    dest.parent.mkdir(parents=True)
    dest.write_text("#!/usr/bin/python3\n")
    dest.chmod(0o755)
    shebang = tmp_path / "python3"
    shebang.write_text("")
    shebang.chmod(0o755)
    monkeypatch.setattr(umu, "SHEBANG_PYTHON", str(shebang))
    _only_python(monkeypatch)
    assert umu.managed() == dest

    dest.chmod(0o644)
    assert umu.managed() is None
    dest.chmod(0o755)
    assert umu.managed() == dest

    monkeypatch.setattr(umu, "SHEBANG_PYTHON", str(tmp_path / "no-such-python3"))
    # No python3 for the shebang: `nadare --umu-run` still runs the zipapp (#1341 item 3).
    assert umu.managed() == dest


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
    # The check ran on the private candidate before it was published (#974).
    assert smoke[0][0][1] != str(dest)


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


def test_ensure_without_python3_still_installs_and_checks_the_zipapp(
    monkeypatch, tmp_path
) -> None:
    """NixOS: no /usr/bin/python3; Nadare's own interpreter runs the check and the game."""
    _fake_download(monkeypatch, _zipapp_payload())
    monkeypatch.setattr(umu.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(umu, "SHEBANG_PYTHON", str(tmp_path / "no-such-python3"))

    dest = umu.ensure_managed()
    assert dest == umu.managed_path() and dest.is_file()


def test_second_ensure_is_a_no_op(monkeypatch) -> None:
    _fake_download(monkeypatch, _payload())
    _fake_smoke(monkeypatch)
    _only_python(monkeypatch)
    dest = umu.ensure_managed()

    monkeypatch.setattr(umu.download, "download", lambda *a, **k: pytest.fail("downloaded again"))
    assert umu.ensure_managed() == dest


def _system_umu(monkeypatch, path: str = "/usr/bin/umu-run") -> None:
    monkeypatch.setattr(
        umu.shutil, "which", lambda name, *a, **k: path if name == "umu-run" else None
    )
    # A system copy that runs and is new enough.
    monkeypatch.setattr(umu, "system", _real_system_with_version(path))


def _real_system_with_version(path: str):
    real = umu.system

    def system(configured="umu-run", **kwargs):
        class Ran:
            returncode = 0
            stdout = "umu-launcher version 1.4.9 (3.13)"
            stderr = ""

        kwargs.setdefault("run", lambda *a, **k: Ran())
        return real(configured, **kwargs)

    return system


def _installed_managed(monkeypatch) -> Path:
    dest = umu.managed_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("#!/usr/bin/python3\n")
    dest.chmod(0o755)
    _only_python(monkeypatch)
    return dest


def test_resolve_prefers_nadares_own_copy_over_a_system_one(monkeypatch) -> None:
    """One tested umu everywhere (user, 2026-11-16): a system umu-run is uncommon and untested."""
    dest = _installed_managed(monkeypatch)
    _system_umu(monkeypatch)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))
    assert umu.resolve(allow_download=True) == str(dest)
    assert umu.resolve(allow_download=False) == str(dest)


def test_resolve_unpacks_the_bundled_archive_before_using_a_system_copy(monkeypatch, tmp_path):
    bundle = tmp_path / "umu.tar"
    bundle.write_bytes(b"x")
    monkeypatch.setattr(umu, "_bundled_archive", lambda: bundle)
    _system_umu(monkeypatch)
    calls: list = []

    def ensure(**kwargs):
        calls.append(kwargs)
        return Path("/data/nadare/umu/1.4.4/umu-run")

    monkeypatch.setattr(umu, "ensure_managed", ensure)
    assert umu.resolve(allow_download=True) == "/data/nadare/umu/1.4.4/umu-run"
    assert len(calls) == 1


def test_resolve_without_permission_does_not_unpack_the_bundle(monkeypatch, tmp_path):
    bundle = tmp_path / "umu.tar"
    bundle.write_bytes(b"x")
    monkeypatch.setattr(umu, "_bundled_archive", lambda: bundle)
    _system_umu(monkeypatch)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("unpacked"))
    assert umu.resolve(allow_download=False) == "/usr/bin/umu-run"


def test_a_bundle_that_cannot_be_set_up_falls_back_to_a_system_copy(monkeypatch, tmp_path):
    bundle = tmp_path / "umu.tar"
    bundle.write_bytes(b"x")
    monkeypatch.setattr(umu, "_bundled_archive", lambda: bundle)
    _system_umu(monkeypatch)

    def boom(**_k):
        raise umu.UmuError("data folder not writable")

    monkeypatch.setattr(umu, "ensure_managed", boom)
    assert umu.resolve(allow_download=True) == "/usr/bin/umu-run"


def test_resolve_uses_a_system_copy_when_nadare_has_none_and_does_not_download(monkeypatch):
    """A source or AUR install with umu-launcher installed: no second umu from the network."""
    _system_umu(monkeypatch)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))
    assert umu.resolve(allow_download=True) == "/usr/bin/umu-run"


def test_a_umu_the_profile_names_wins_over_nadares_own(monkeypatch) -> None:
    _installed_managed(monkeypatch)
    monkeypatch.setattr(
        umu.shutil, "which", lambda name, *a, **k: name if name == "/opt/my/umu-run" else None
    )
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))
    assert umu.resolve("/opt/my/umu-run", allow_download=True) == "/opt/my/umu-run"


def test_with_nothing_at_all_nadares_copy_is_downloaded(monkeypatch) -> None:
    monkeypatch.setattr(umu.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: Path("/d/umu-run"))
    assert umu.resolve(allow_download=True) == "/d/umu-run"


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
    exe = write_exe(prefix / ak.DEFAULT_INSTALL_SUBDIR / "Arknights.exe", "MZ")
    proton = tmp_path / "Proton"
    proton.mkdir()
    (proton / "proton").write_text("")
    monkeypatch.setattr(wg.runners, "resolve_runner", lambda value: proton)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: missing)
    backend = ak.ArknightsBackend(
        make_context(tmp_path, ak.KIND, {"prefix": str(prefix), "runner": "MyProton"})
    )
    return backend, proton, exe


def test_system_umu_is_used_without_downloading(tmp_path, monkeypatch) -> None:
    backend, _proton, exe = _ak_backend(tmp_path, monkeypatch, ["libfreetype.so.6"])
    monkeypatch.setattr(
        wg.shutil, "which", lambda name, *a, **k: "/opt/umu-run" if name == "umu-run" else None
    )
    monkeypatch.setattr(umu, "ensure_managed", lambda **k: pytest.fail("downloaded"))

    argv, _env, _target = backend.build_launch()
    assert argv == ["/opt/umu-run", str(exe)]


def test_working_32bit_libs_still_go_through_the_managed_umu(tmp_path, monkeypatch) -> None:
    """#1338 (user): this ran `proton run`, which skipped every per-game fix."""
    backend, _proton, exe = _ak_backend(tmp_path, monkeypatch, [])
    calls: list = []
    _fake_download(monkeypatch, _payload(), calls)
    _fake_smoke(monkeypatch)
    monkeypatch.setattr(
        wg.shutil,
        "which",
        lambda name, *a, **k: "/usr/bin/python3" if name == "python3" else None,
    )

    argv, _env, _target = backend.build_launch()
    assert argv == [str(umu.managed_path()), str(exe)]
    assert len(calls) == 1


def test_missing_libs_and_no_system_umu_downloads_once_and_uses_managed(
    tmp_path, monkeypatch
) -> None:
    backend, _proton, exe = _ak_backend(tmp_path, monkeypatch, ["libfreetype.so.6"])
    calls: list = []
    _fake_download(monkeypatch, _payload(), calls)
    _fake_smoke(monkeypatch)
    monkeypatch.setattr(
        wg.shutil,
        "which",
        lambda name, *a, **k: "/usr/bin/python3" if name == "python3" else None,
    )

    argv, _env, _target = backend.build_launch()
    assert argv == [str(umu.managed_path()), str(exe)]
    assert len(calls) == 1


def test_download_failure_explains_how_to_get_umu(tmp_path, monkeypatch) -> None:
    backend, _proton, _exe = _ak_backend(tmp_path, monkeypatch, ["libfreetype.so.6"])

    def offline(*_a, **_k):
        raise dl.DownloadError("offline")

    monkeypatch.setattr(umu.download, "download", offline)
    _fake_smoke(monkeypatch)
    monkeypatch.setattr(
        wg.shutil,
        "which",
        lambda name, *a, **k: "/usr/bin/python3" if name == "python3" else None,
    )

    with pytest.raises(BackendError) as excinfo:
        backend.build_launch()
    # umu brings its own libraries, so the host's missing ones no longer matter (#1338).
    assert "umu-launcher" in str(excinfo.value)


def test_flatpak_runs_an_unsandboxed_game_through_the_managed_umu(tmp_path, monkeypatch) -> None:
    """Inside the Flatpak, Proton runs in umu's Steam Runtime like in the AppImage (measured:
    on the bare Flatpak runtime a game's intro video fails). Arknights has no Sandbox."""
    backend, proton, exe = _ak_backend(tmp_path, monkeypatch, [])
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: True)
    monkeypatch.setattr(wg.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(umu, "system", lambda *_a, **_k: None)
    monkeypatch.setattr(umu, "resolve", lambda *_a, allow_download, **_k: "/managed/umu-run")

    argv, _env, _target = backend.build_launch()
    assert argv == ["/managed/umu-run", str(exe)]


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
    monkeypatch.setattr(wg.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
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
    monkeypatch.setattr(wg.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
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

    monkeypatch.setattr(wg.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
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

    monkeypatch.setattr(wg.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr(wg.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setattr(wg.hostlibs, "missing_32bit", lambda: ["libfreetype.so.6"])
    monkeypatch.setattr(umu, "ensure_managed", ensure)

    argv, _env = backend._prefix_command(["game.exe"])
    assert argv == [str(missing), "game.exe"]
    assert calls == [1]
