# SPDX-License-Identifier: GPL-3.0-or-later
"""The pinned lepton-fgo installer download and the host preflight hints.

Nothing here touches the network: ``fetch`` is injected and copies a tarball
built in the test. Building it needs the ``zstd`` binary (as the real
installer does); the extraction tests are skipped without it.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from _backend_helpers import make_context

from nadare.backends.fgo import installer, lepton
from nadare.backends.fgo.backend import FgoLeptonBackend
from nadare.core import paths
from nadare.core.backends import BackendError
from nadare.core.events import Cancel, Cancelled

needs_zstd = pytest.mark.skipif(shutil.which("zstd") is None, reason="zstd not installed")

VERSION = "9.9.9-test"


@pytest.fixture(autouse=True)
def _index(monkeypatch):
    """The suite disables the index by default; these tests need a base URL."""
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://idx.example/")


def _tarball(tmp_path: Path, members: dict[str, bytes], version: str = VERSION) -> Path:
    """Build ``lepton-fgo-<version>.tar.zst`` with ``members`` (path -> bytes)."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    out = tmp_path / f"lepton-fgo-{version}.tar.zst"
    out.write_bytes(
        subprocess.run(
            ["zstd", "-q", "-c"], input=raw.getvalue(), capture_output=True, check=True
        ).stdout
    )
    return out


def _good(tmp_path: Path, version: str = VERSION) -> Path:
    top = f"lepton-fgo-{version}"
    return _tarball(
        tmp_path,
        {
            f"{top}/install.sh": b"#!/bin/sh\necho install\n",
            f"{top}/lepton/check-host.sh": b"#!/bin/sh\necho ok\n",
            f"{top}/RELEASE": f"lepton-fgo {version}\n".encode(),
        },
        version,
    )


def _pin(path: Path, version: str = VERSION, sha: str | None = None) -> installer.InstallerPin:
    digest = sha or hashlib.sha256(path.read_bytes()).hexdigest()
    return installer.InstallerPin(version=version, file=path.name, sha256=digest)


class FakeFetch:
    """Stands in for core.download.download: copies ``src`` and checks the sha."""

    def __init__(self, src: Path) -> None:
        self.src = src
        self.calls: list[str] = []

    def __call__(self, url: str, dest: Path, expected_sha256: str) -> None:
        self.calls.append(url)
        data = self.src.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise installer.IntegrityError("sha256 mismatch")  # what download() raises
        Path(dest).write_bytes(data)


def _adapter(tmp_path: Path, repo: Path | None = None) -> lepton.LeptonFgo:
    return lepton.LeptonFgo(prefix=tmp_path / "prefix", repo=repo)


# ---- URL ------------------------------------------------------------------
def test_url_comes_from_the_index(monkeypatch) -> None:
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://idx.example/raw")
    pin = installer.InstallerPin("1.0", "lepton-fgo-1.0.tar.zst", "0" * 64)
    assert installer.installer_url(pin) == "https://idx.example/raw/lepton/lepton-fgo-1.0.tar.zst"


def test_pin_is_a_real_sha256() -> None:
    assert len(installer.PIN.sha256) == 64
    assert installer.PIN.file == f"lepton-fgo-{installer.PIN.version}.tar.zst"


# ---- ensure_installer -----------------------------------------------------
@needs_zstd
def test_downloads_verifies_and_extracts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NADARE_INDEX_BASE", "https://idx.example/")
    tar = _good(tmp_path)
    fetch = FakeFetch(tar)
    got = installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=fetch)

    assert got == paths.data_dir() / "lepton-fgo" / VERSION
    assert (got / "install.sh").is_file()
    assert (got / "lepton" / "check-host.sh").is_file()
    assert (got / "RELEASE").read_text().strip() == f"lepton-fgo {VERSION}"
    assert fetch.calls == [f"https://idx.example/lepton/{tar.name}"]
    # No temporary files are left next to the result.
    assert sorted(p.name for p in got.parent.iterdir()) == [VERSION]


@needs_zstd
def test_second_call_reuses_the_extracted_installer(tmp_path: Path) -> None:
    tar = _good(tmp_path)
    fetch = FakeFetch(tar)
    first = installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=fetch)
    second = installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=fetch)
    assert first == second
    assert len(fetch.calls) == 1


@needs_zstd
def test_extraction_without_a_stamp_is_not_trusted(tmp_path: Path) -> None:
    """L2: the version stamp alone must not authorise executing a reused copy."""
    tar = _good(tmp_path)
    dest = paths.data_dir() / "lepton-fgo" / VERSION
    dest.mkdir(parents=True)
    (dest / "install.sh").write_text("#!/bin/sh\necho hostile\n")
    (dest / "RELEASE").write_text(f"lepton-fgo {VERSION}\n")

    fetch = FakeFetch(tar)
    got = installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=fetch)
    assert len(fetch.calls) == 1, "an unstamped copy must be re-downloaded"
    assert (got / "install.sh").read_text() == "#!/bin/sh\necho install\n"


@needs_zstd
def test_mutated_extraction_is_redownloaded(tmp_path: Path) -> None:
    """L2: changing install.sh after extraction invalidates the stamp."""
    tar = _good(tmp_path)
    fetch = FakeFetch(tar)
    first = installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=fetch)
    (first / "install.sh").write_text("#!/bin/sh\necho tampered\n")

    second = installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=fetch)
    assert second == first
    assert len(fetch.calls) == 2
    assert (second / "install.sh").read_text() == "#!/bin/sh\necho install\n"


@needs_zstd
def test_symlinked_extraction_is_replaced_not_followed(tmp_path: Path) -> None:
    """F3: a pre-existing ``dest`` symlink must be swapped out, never executed."""
    tar = _good(tmp_path)
    dest = paths.data_dir() / "lepton-fgo" / VERSION
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "install.sh").write_text("#!/bin/sh\necho hostile\n")
    (target / "RELEASE").write_text(f"lepton-fgo {VERSION}\n")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(target, target_is_directory=True)

    got = installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=FakeFetch(tar))
    assert got == dest
    assert not dest.is_symlink()
    assert (dest / "install.sh").read_text() == "#!/bin/sh\necho install\n"
    assert (target / "install.sh").read_text() == "#!/bin/sh\necho hostile\n"


def test_cancel_during_download_propagates(tmp_path: Path) -> None:
    """F2: a Cancelled from the fetch seam must not become a LeptonError."""

    def fetch(url: str, dest_path: Path, sha: str) -> None:
        raise Cancelled("stop")

    with pytest.raises(Cancelled):
        installer.ensure_installer(
            _adapter(tmp_path), pin=installer.PIN, fetch=fetch, cancel=Cancel()
        )
    assert not (paths.data_dir() / "lepton-fgo" / installer.PIN.version).exists()


@needs_zstd
def test_sha_mismatch_is_refused_and_leaves_nothing(tmp_path: Path) -> None:
    tar = _good(tmp_path)
    with pytest.raises(lepton.LeptonError, match="sha256|integrity|verif"):
        installer.ensure_installer(
            _adapter(tmp_path), pin=_pin(tar, sha="a" * 64), fetch=FakeFetch(tar)
        )
    assert not (paths.data_dir() / "lepton-fgo" / VERSION).exists()


@needs_zstd
def test_path_traversal_member_is_refused(tmp_path: Path) -> None:
    top = f"lepton-fgo-{VERSION}"
    tar = _tarball(tmp_path, {f"{top}/install.sh": b"x", "../evil.sh": b"x"})
    with pytest.raises(lepton.LeptonError):
        installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=FakeFetch(tar))
    assert not (tmp_path / "evil.sh").exists()
    assert not (paths.data_dir() / "evil.sh").exists()
    assert not (paths.data_dir() / "lepton-fgo" / VERSION).exists()


@needs_zstd
def test_tarball_without_install_sh_is_refused(tmp_path: Path) -> None:
    tar = _tarball(tmp_path, {f"lepton-fgo-{VERSION}/README.md": b"x"})
    with pytest.raises(lepton.LeptonError, match="install.sh"):
        installer.ensure_installer(_adapter(tmp_path), pin=_pin(tar), fetch=FakeFetch(tar))


def test_explicit_checkout_wins_and_nothing_is_fetched(tmp_path: Path) -> None:
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "install.sh").write_text("#!/bin/sh\n")
    fetch = FakeFetch(tmp_path / "missing.tar.zst")
    got = installer.ensure_installer(_adapter(tmp_path, repo=repo), pin=installer.PIN, fetch=fetch)
    assert got == repo
    assert fetch.calls == []


def test_disabled_index_without_checkout_is_a_clear_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("NADARE_INDEX_BASE", "")
    monkeypatch.delenv("LEPTON_FGO_REPO", raising=False)
    with pytest.raises(lepton.LeptonError, match="lepton-fgo installer"):
        installer.ensure_installer(
            _adapter(tmp_path), pin=installer.PIN, fetch=FakeFetch(tmp_path / "x")
        )


def test_adapter_uses_the_ensured_installer(tmp_path: Path) -> None:
    repo = tmp_path / "dl"
    (repo / "lepton").mkdir(parents=True)
    (repo / "install.sh").write_text("#!/bin/sh\n")
    (repo / "lepton" / "check-host.sh").write_text("#!/bin/sh\n")
    adapter = _adapter(tmp_path)
    adapter.set_repo(repo)
    assert adapter.repo == repo
    assert adapter.install_sh == repo / "install.sh"
    assert adapter.check_host_sh == repo / "lepton" / "check-host.sh"


# ---- install hints ----------------------------------------------------------
MISSING = [
    "crun not found in PATH",
    "inotifywait not found in PATH",
    "java not found in PATH",
    "lzip not found in PATH",
]


def test_hint_debian_family() -> None:
    hint = installer.install_hint(MISSING, {"ID": "linuxmint", "ID_LIKE": "ubuntu debian"})
    assert "sudo apt-get install -y crun inotify-tools default-jre-headless lzip" in hint


def test_hint_fedora() -> None:
    hint = installer.install_hint(MISSING, {"ID": "fedora"})
    assert "sudo dnf install -y crun inotify-tools java-21-openjdk-headless lzip" in hint


def test_hint_fedora_atomic_uses_rpm_ostree() -> None:
    for release in (
        {"ID": "bazzite", "ID_LIKE": "fedora"},
        {"ID": "fedora", "VARIANT_ID": "kinoite"},
    ):
        hint = installer.install_hint(["podman not found in PATH"], release)
        assert "rpm-ostree install podman" in hint, release


def test_hint_arch_family() -> None:
    hint = installer.install_hint(MISSING, {"ID": "cachyos", "ID_LIKE": "arch"})
    assert "sudo pacman -S --needed crun inotify-tools jre-openjdk-headless lzip" in hint


def test_hint_unknown_distro_lists_packages() -> None:
    hint = installer.install_hint(
        ["podman not found in PATH", "zip not found in PATH"], {"ID": "plan9"}
    )
    assert "podman" in hint and "zip" in hint
    assert "sudo" not in hint


def test_hint_binderfs_is_a_kernel_problem_not_a_package() -> None:
    hint = installer.install_hint(
        ["binderfs missing (kernel needs CONFIG_ANDROID_BINDERFS)"], {"ID": "debian"}
    )
    assert "binderfs" in hint and "kernel" in hint
    assert "apt-get" not in hint


def test_hint_empty_when_nothing_mappable() -> None:
    assert installer.install_hint([], {"ID": "fedora"}) == ""


# ---- backend wiring ---------------------------------------------------------
def test_backend_install_reports_missing_with_hint(tmp_path: Path, monkeypatch) -> None:
    ctx = make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    backend = FgoLeptonBackend(ctx)
    monkeypatch.setattr(installer, "ensure_installer", lambda adapter, **k: tmp_path)
    monkeypatch.setattr(
        backend.adapter,
        "check_host",
        lambda: {
            "ok": False,
            "missing": ["crun not found in PATH"],
            "selinux_enforcing": False,
            "output": "",
        },
    )
    monkeypatch.setattr(installer, "os_release", lambda: {"ID": "fedora"})
    with pytest.raises(BackendError) as exc:
        backend.install()
    text = str(exc.value)
    assert "crun" in text and "sudo dnf install -y crun" in text


def test_backend_install_fetches_installer_before_host_check(tmp_path: Path, monkeypatch) -> None:
    ctx = make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    backend = FgoLeptonBackend(ctx)
    order: list[str] = []

    def fake_ensure(adapter, **k):
        order.append("ensure")
        return tmp_path / "dl"

    def fake_check():
        order.append("check")
        return {"ok": False, "missing": [], "selinux_enforcing": False, "output": ""}

    monkeypatch.setattr(installer, "ensure_installer", fake_ensure)
    monkeypatch.setattr(backend.adapter, "check_host", fake_check)
    with pytest.raises(BackendError):
        backend.install()
    assert order == ["ensure", "check"]
    assert backend.adapter.repo == tmp_path / "dl" or backend.adapter._repo_arg == str(
        tmp_path / "dl"
    )


def test_backend_install_turns_installer_error_into_backend_error(
    tmp_path: Path, monkeypatch
) -> None:
    ctx = make_context(tmp_path, "fgo-lepton", {"accept_account_notice": True})
    backend = FgoLeptonBackend(ctx)

    def boom(adapter, **k):
        raise lepton.LeptonError("lepton-fgo installer download failed")

    monkeypatch.setattr(installer, "ensure_installer", boom)
    with pytest.raises(BackendError, match="lepton-fgo installer"):
        backend.install()


def test_os_release_prefers_the_host_copy_in_a_flatpak(tmp_path: Path) -> None:
    host = tmp_path / "host-os-release"
    host.write_text('ID=bazzite\nID_LIKE="fedora"\n')
    runtime = tmp_path / "etc-os-release"
    runtime.write_text("ID=org.freedesktop.platform\n")
    assert installer.os_release((str(host), str(runtime)))["ID"] == "bazzite"
    assert (
        installer.os_release((str(tmp_path / "nope"), str(runtime)))["ID"]
        == "org.freedesktop.platform"
    )


def test_fgo_page_says_experimental() -> None:
    assert "Experimental" in FgoLeptonBackend.page_notice[0]


def test_hint_covers_the_slim_requirements() -> None:
    hint = installer.install_hint(
        ["python3 not found in PATH", "curl not found in PATH", "zstd not found in PATH"],
        {"ID": "ubuntu", "ID_LIKE": "debian"},
    )
    assert "sudo apt-get install -y python3 curl zstd" in hint


def test_hint_arch_maps_python3_to_python() -> None:
    hint = installer.install_hint(["python3 not found in PATH"], {"ID": "arch"})
    assert hint.endswith("--needed python")
