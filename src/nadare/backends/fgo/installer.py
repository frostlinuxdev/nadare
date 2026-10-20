# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Pinned ``lepton-fgo`` installer download and host preflight hints.

FGO runs through the external ``lepton-fgo`` project.  On a fresh machine there
is no checkout to install from, so this module downloads a pinned release
tarball from the Nadare index, verifies its sha256, unpacks it and hands the
resulting directory to the adapter.  A user checkout (or ``$LEPTON_FGO_REPO``)
always wins over the download.

The second half turns the host check's ``MISSING`` lines into a distro-specific
install command so the user knows what to run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...core import hosting, paths
from ...core.download import IntegrityError, download, hash_file
from ...core.events import Cancel, Cancelled, Event, emitter
from .lepton import LeptonError, LeptonFgo

__all__ = [
    "IntegrityError",
    "PIN",
    "InstallerPin",
    "ensure_installer",
    "install_hint",
    "installer_url",
    "lepton_url",
    "os_release",
]


#: Records which pinned tarball produced an extracted copy (and the hash of its
#: ``install.sh``). Reuse verifies both, so a mutated extraction is re-downloaded
#: instead of being executed.
_STAMP = ".nadare-installer.json"


@dataclass(frozen=True)
class InstallerPin:
    """The one lepton-fgo release Nadare ships against."""

    version: str
    file: str
    sha256: str


PIN = InstallerPin(
    version="1.1.0-beta.2",
    file="lepton-fgo-1.1.0-beta.2.tar.zst",
    sha256="2c5d70340fa67553351fbd0a377d41097e52dffe7de3d583dabe2f09e99b062e",
)


def lepton_url(name: str) -> str:
    """Raw URL of a file under ``lepton/`` in the index (``""`` when disabled)."""
    base = hosting.index_base()
    return f"{base}lepton/{name}" if base else ""


def installer_url(pin: InstallerPin = PIN) -> str:
    """Raw URL of the pinned tarball (``""`` when the index is disabled)."""
    return lepton_url(pin.file)


def _install_sh_sha256(dest: Path) -> str | None:
    try:
        return hash_file(dest / "install.sh", ("sha256",))["sha256"]
    except OSError:
        return None


def _stamp_matches(dest: Path, pin: InstallerPin) -> bool:
    """True when ``dest``'s stamp ties it to ``pin`` and is unchanged."""
    try:
        stamp = json.loads((dest / _STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(stamp, dict) or stamp.get("tarball_sha256") != pin.sha256:
        return False
    actual = _install_sh_sha256(dest)
    return actual is not None and stamp.get("install_sh_sha256") == actual


def _write_stamp(dest: Path, pin: InstallerPin) -> None:
    """Record the pin so reuse can verify it; best-effort (worst case: redownload)."""
    stamp = {"tarball_sha256": pin.sha256, "install_sh_sha256": _install_sh_sha256(dest)}
    try:
        (dest / _STAMP).write_text(json.dumps(stamp) + "\n", encoding="utf-8")
    except OSError:
        pass


def ensure_installer(
    adapter: LeptonFgo,
    *,
    pin: InstallerPin = PIN,
    fetch: Callable[[str, Path, str], None] | None = None,
    emit=None,  # noqa: ANN001 - Emitter | None
    cancel: Cancel | None = None,
) -> Path:
    """Return a directory with a verified ``install.sh`` for ``pin``.

    Order of preference: a user checkout, an already-extracted copy, then the
    pinned tarball from the index.
    """
    repo_arg = getattr(adapter, "_repo_arg", None)
    if repo_arg:
        repo = Path(repo_arg)
        if (repo / "install.sh").is_file():
            return repo

    dest = paths.data_dir() / "lepton-fgo" / pin.version
    install_sh = dest / "install.sh"
    release_file = dest / "RELEASE"
    if install_sh.is_file() and release_file.is_file():
        if (
            release_file.read_text(encoding="utf-8").strip() == f"lepton-fgo {pin.version}"
            and _stamp_matches(dest, pin)
        ):
            return dest

    url = installer_url(pin)
    if not url:
        raise LeptonError(
            "lepton-fgo installer is not available: "
            "the Nadare index is disabled and no local checkout was found"
        )

    if fetch is None:

        def fetch(url: str, dest_path: Path, sha: str) -> None:
            download(url, dest_path, expected_sha256=sha, emit=emit, cancel=cancel)

    say = emitter(emit)
    say(Event("step", f"downloading lepton-fgo installer {pin.version}"))

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=".tmp-"))
    try:
        archive = tmp / pin.file
        try:
            fetch(url, archive, pin.sha256)
        except IntegrityError as exc:
            raise LeptonError(f"lepton-fgo installer failed sha256 verification: {exc}") from exc
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - any fetch failure is fatal
            raise LeptonError(f"lepton-fgo installer download failed: {exc}") from exc

        tar_path = tmp / "installer.tar"
        try:
            with open(tar_path, "wb") as out:
                subprocess.run(
                    ["zstd", "-q", "-d", "-c", str(archive)],
                    stdout=out,
                    check=True,
                )
        except FileNotFoundError as exc:
            raise LeptonError("zstd is needed to unpack the lepton-fgo installer") from exc
        except subprocess.CalledProcessError as exc:
            raise LeptonError(f"zstd failed to unpack the lepton-fgo installer: {exc}") from exc

        extract_dir = tmp / "x"
        extract_dir.mkdir()
        try:
            with tarfile.open(tar_path) as tar:
                tar.extractall(extract_dir, filter="data")
        except tarfile.TarError as exc:
            raise LeptonError(f"lepton-fgo installer tarball is not safe: {exc}") from exc

        top = extract_dir / f"lepton-fgo-{pin.version}"
        if not (top / "install.sh").is_file():
            raise LeptonError("lepton-fgo installer tarball has no install.sh")

        try:
            if dest.is_symlink():
                dest.unlink()
            elif dest.exists():
                shutil.rmtree(dest)
            os.replace(top, dest)
        except OSError as exc:
            raise LeptonError(f"could not place the lepton-fgo installer at {dest}: {exc}") from exc
        _write_stamp(dest, pin)
        return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# host preflight hints
# ---------------------------------------------------------------------------
_PACKAGE_BY_WORD = {
    "podman": "podman",
    "crun": "crun",
    "catatonit": "catatonit",
    "attr": "attr",
    "getfattr": "attr",
    "setfattr": "attr",
    "inotifywait": "inotify-tools",
    "zip": "zip",
    "unzip": "unzip",
    "lzip": "lzip",
    "python3": "python3",
    "curl": "curl",
    "tar": "tar",
    "zstd": "zstd",
}

_JAVA_BY_FAMILY = {
    "debian": "default-jre-headless",
    "fedora": "java-21-openjdk-headless",
    "arch": "jre-openjdk-headless",
    "suse": "java-21-openjdk-headless",
}

_BINDERFS_HINT = (
    "Your kernel has no binderfs (CONFIG_ANDROID_BINDERFS), which FGO's Android container needs. "
    "Use a kernel that has it (e.g. Fedora, Bazzite, CachyOS); stock Debian kernels do not."
)

_ATOMIC_VARIANTS = {"silverblue", "kinoite", "sericea", "onyx", "cosmic-atomic"}


def _family(release: dict[str, str]) -> str:
    ids: set[str] = set()
    for key in ("ID", "ID_LIKE"):
        ids.update(str(release.get(key) or "").lower().split())
    if ids & {"debian", "ubuntu", "linuxmint", "pop"}:
        return "debian"
    if ids & {"fedora", "rhel", "centos", "bazzite", "nobara"}:
        return "fedora"
    if ids & {"arch", "cachyos", "endeavouros", "manjaro"}:
        return "arch"
    if ids & {"opensuse", "suse", "opensuse-tumbleweed", "opensuse-leap"}:
        return "suse"
    return ""


def _command(family: str, release: dict[str, str], packages: list[str]) -> str:
    pkgs = " ".join(packages)
    if family == "debian":
        return f"sudo apt-get install -y {pkgs}"
    if family == "fedora":
        variant = str(release.get("VARIANT_ID") or "")
        atomic = (
            release.get("ID") == "bazzite" or variant in _ATOMIC_VARIANTS or "ostree" in variant
        )
        if atomic:
            return f"rpm-ostree install {pkgs}"
        return f"sudo dnf install -y {pkgs}"
    if family == "arch":
        return f"sudo pacman -S --needed {pkgs}"
    if family == "suse":
        return f"sudo zypper install {pkgs}"
    return f"Install these packages: {pkgs}"


def install_hint(missing: list[str], release: dict[str, str]) -> str:
    """Turn host-check ``MISSING`` lines into a distro install command."""
    family = _family(release)
    packages: list[str] = []
    binderfs = False
    for line in missing:
        text = str(line)
        if "binderfs" in text.lower():
            binderfs = True
            continue
        words = text.split()
        if not words:
            continue
        word = words[0].lower()
        if word == "java":
            package = _JAVA_BY_FAMILY.get(family, "java")
        else:
            package = _PACKAGE_BY_WORD.get(word)
            if word == "python3" and family == "arch":
                package = "python"
        if package and package not in packages:
            packages.append(package)

    lines: list[str] = []
    if packages:
        lines.append(_command(family, release, packages))
    if binderfs:
        lines.append(_BINDERFS_HINT)
    return "\n".join(lines)


def os_release(
    paths_: tuple[str, ...] = ("/run/host/os-release", "/etc/os-release"),
) -> dict[str, str]:
    """Parse the host's os-release into ``KEY=VALUE`` pairs (``{}`` on error).

    Inside a Flatpak ``/etc/os-release`` describes the runtime, so the host's
    copy under ``/run/host`` is preferred when it exists.
    """
    path = next((p for p in paths_ if os.path.isfile(p)), paths_[-1])
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                out[key.strip()] = val
    except OSError:
        return {}
    return out
