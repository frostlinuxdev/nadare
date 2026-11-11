#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Stage the AppImage payload as a Flatpak build directory for our own repository.

    python3 packaging/flatpak/stage_repo.py --appdir AppDir --out build/flatpak-root

Uses the trimmed AppImage payload (``AppDir/usr/bin`` from ``scripts/build_appimage.sh``),
keeping symlinks so Qt is stored once, and applies the manifest's own rules: the same
libraries removed in favour of the runtime's, the same permissions and extension points.
``build_repo.sh`` then composes the store catalogue, finishes, signs and exports it.

It also stages the pinned Debian libraries Proton needs but the Freedesktop runtime lacks
(``proton-libs.json``). Each package is fetched into ``--deb-cache`` (default
``build/flatpak-debs``) and verified against its SHA-256, then unpacked with pure Python:
regular files and symlinks under ``usr/lib/<multiarch>/`` whose names contain ``.so`` go into
``files/lib/nadare-proton/x86_64`` or ``i386``, and ``usr/share/doc/<pkg>/copyright`` into
``files/share/licenses/<APP_ID>/proton-libs``. Only Proton is pointed at that directory;
Nadare keeps using the runtime's libraries. ``--no-proton-libs`` skips this step.

The 32-bit ``Compat.i386`` / ``GL32.*`` extensions are not copied into our repository:
Flathub hosts them, and Nadare installs any that are missing on request
(``core/flatpakgl.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APP_ID = "io.github.frostlinuxdev.Nadare"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, target: Path) -> None:
    """Download an HTTPS URL to ``target``."""
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError(f"refusing a non-HTTPS URL: {url}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as response, target.open("wb") as stream:
        shutil.copyfileobj(response, stream)


def fetch_deb(entry: dict, cache: Path) -> Path:
    """Return the cached package, downloading and checksumming it when needed."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / entry["url"].rsplit("/", 1)[-1]
    expected = entry["sha256"]
    if target.is_file() and _sha256(target) == expected:
        return target
    handle, temporary = tempfile.mkstemp(dir=cache, prefix=f".{target.name}.", suffix=".part")
    os.close(handle)
    temporary = Path(temporary)
    try:
        _download(entry["url"], temporary)
        actual = _sha256(temporary)
        if actual != expected:
            raise SystemExit(
                f"{target.name}: sha256 mismatch: expected {expected}, got {actual}"
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _ar_members(blob: bytes):
    """Yield ``(name, data)`` for each member of an ``ar`` archive."""
    if not blob.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive")
    offset = 8
    while offset + 60 <= len(blob):
        header = blob[offset : offset + 60]
        if header[58:60] != b"`\n":
            raise ValueError("bad ar member header")
        name = header[:16].decode("utf-8", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        start = offset + 60
        end = start + size
        if end > len(blob):
            raise ValueError("truncated ar member")
        yield name, blob[start:end]
        offset = end + (size % 2)


def _data_tar(deb: Path) -> bytes:
    for name, member in _ar_members(deb.read_bytes()):
        if name.startswith("data.tar"):
            return member
    raise ValueError(f"{deb.name}: no data.tar member")


def _write_file(target: Path, content: bytes) -> None:
    if target.is_symlink():
        target.unlink()
    target.write_bytes(content)


def extract_deb(deb: Path, libdir: Path, licences: Path) -> None:
    """Unpack one package's libraries and copyright file without dpkg."""
    deb = Path(deb)
    libdir = Path(libdir)
    licences = Path(licences)
    package = deb.name.split("_", 1)[0]
    libdir.mkdir(parents=True, exist_ok=True)
    copyright_name = f"usr/share/doc/{package}/copyright"
    with tarfile.open(fileobj=io.BytesIO(_data_tar(deb)), mode="r:*") as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name).as_posix()
            parts = PurePosixPath(name).parts
            if name == copyright_name and member.isfile():
                source = tar.extractfile(member)
                if source is not None:
                    target = licences / package / "copyright"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _write_file(target, source.read())
                continue
            if not (
                len(parts) >= 4
                and parts[:2] == ("usr", "lib")
                and ".so" in parts[-1]
            ):
                continue
            target = libdir / parts[-1]
            if member.isfile():
                source = tar.extractfile(member)
                if source is not None:
                    _write_file(target, source.read())
            elif member.issym():
                link = member.linkname
                if link.startswith("/") or ".." in link or "/" in link:
                    raise ValueError(f"{deb.name}: refusing symlink {parts[-1]} -> {link}")
                if target.is_symlink() or target.exists():
                    target.unlink()
                target.symlink_to(link)


def stage_proton_libs(files: Path, cache: Path) -> None:
    """Stage the pinned Proton libraries and their licences under ``files``."""
    entries = json.loads((HERE / "proton-libs.json").read_text())
    licences = files / "share" / "licenses" / APP_ID / "proton-libs"
    for entry in entries:
        deb = fetch_deb(entry, cache)
        arch = "x86_64" if entry["arch"] == "amd64" else "i386"
        extract_deb(deb, files / "lib" / "nadare-proton" / arch, licences)
    for arch in ("x86_64", "i386"):
        # A Kerberos plugin, not a library Proton loads.
        (files / "lib" / "nadare-proton" / arch / "spake.so").unlink(missing_ok=True)


def finish_argv(manifest: dict, build_dir: str) -> list[str]:
    """``flatpak build-finish`` arguments equivalent to the manifest."""
    argv = ["flatpak", "build-finish", f"--command={manifest['command']}"]
    argv += list(manifest.get("finish-args", []))
    for extension, values in manifest.get("add-extensions", {}).items():
        for key, value in values.items():
            if isinstance(value, bool):
                value = str(value).lower()
            argv.append(f"--extension={extension}={key}={value}")
    argv.append(build_dir)
    return argv


def removed_libraries(manifest: dict) -> list[str]:
    """The bundled libraries the manifest deletes in favour of the runtime's copies."""
    for module in manifest["modules"]:
        for command in module.get("build-commands", []):
            if "rm -f" in command:
                return shlex.split(command.split("rm -f", 1)[1])
    return []


def render_icon(svg: Path, size: int, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    for tool in (
        ["rsvg-convert", "-w", str(size), "-h", str(size), str(svg), "-o", str(target)],
        ["magick", "-background", "none", str(svg), "-resize", f"{size}x{size}", str(target)],
    ):
        if shutil.which(tool[0]):
            subprocess.run(tool, check=True)
            return
    raise SystemExit("need rsvg-convert or ImageMagick (magick) to render the icon")


def stage(
    appdir: Path,
    out: Path,
    *,
    proton_libs: bool = True,
    deb_cache: Path | None = None,
) -> None:
    manifest = yaml.safe_load((HERE / f"{APP_ID}.yml").read_text())
    if out.exists():
        raise SystemExit(f"{out} exists; use a fresh directory")
    payload = appdir / "usr" / "bin"
    if not (payload / "nadare").is_file():
        raise SystemExit(f"no AppImage payload at {payload} (run scripts/build_appimage.sh)")
    files = out / "files"
    shutil.copytree(payload, files / "lib" / "nadare", symlinks=True)
    internal = files / "lib" / "nadare" / "_internal"
    for name in removed_libraries(manifest):
        target = internal / name
        if target.parent == internal:
            target.unlink(missing_ok=True)
    (files / "bin").mkdir()
    (files / "bin" / "nadare").symlink_to("../lib/nadare/nadare")
    (files / "lib" / "i386-linux-gnu" / "GL").mkdir(parents=True)
    ld_conf = manifest["modules"][0]["sources"][0]["contents"]
    (files / "etc").mkdir()
    (files / "etc" / "ld.so.conf").write_text(ld_conf)

    entry = (ROOT / "packaging" / "nadare.desktop").read_text().splitlines()
    entry = [f"Icon={APP_ID}" if line.startswith("Icon=") else line for line in entry]
    if not any(line.startswith("StartupWMClass=") for line in entry):
        entry.append("StartupWMClass=nadare")
    apps = files / "share" / "applications"
    apps.mkdir(parents=True)
    (apps / f"{APP_ID}.desktop").write_text("\n".join(entry) + "\n")
    for size in (128, 256, 512):
        render_icon(
            ROOT / "packaging" / "nadare.svg",
            size,
            files / "share" / "icons" / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png",
        )
    meta = files / "share" / "metainfo"
    meta.mkdir(parents=True)
    shutil.copy2(HERE / f"{APP_ID}.metainfo.xml", meta)
    licence = files / "share" / "licenses" / APP_ID / "LICENSE"
    licence.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "LICENSE", licence)
    if proton_libs:
        stage_proton_libs(files, deb_cache or ROOT / "build" / "flatpak-debs")

    runtime = f"{manifest['runtime']}/x86_64/{manifest['runtime-version']}"
    sdk = f"{manifest['sdk']}/x86_64/{manifest['runtime-version']}"
    (out / "metadata").write_text(
        f"[Application]\nname={APP_ID}\nruntime={runtime}\nsdk={sdk}\n"
        f"command={manifest['command']}\n"
    )
    (out / "finish-argv.json").write_text(json.dumps(finish_argv(manifest, out.name)))
    print(f"staged {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--appdir", type=Path, default=ROOT / "AppDir")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--deb-cache",
        type=Path,
        default=ROOT / "build" / "flatpak-debs",
        help="where the pinned Debian packages are cached (default: %(default)s)",
    )
    parser.add_argument(
        "--no-proton-libs",
        dest="proton_libs",
        action="store_false",
        help="skip the pinned Proton libraries and their licences",
    )
    args = parser.parse_args(argv)
    stage(args.appdir, args.out, proton_libs=args.proton_libs, deb_cache=args.deb_cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
