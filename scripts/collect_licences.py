# SPDX-License-Identifier: GPL-3.0-or-later
"""Collect the licence texts of everything the AppImage bundles (#587).

Run at build time with the build interpreter:

    python scripts/collect_licences.py --internal AppDir/usr/bin/_internal \
        --toc build/nadare/PYZ-00.toc --out AppDir/usr/share/licenses/nadare/third-party

Python wheels are read from their installed metadata; bundled system libraries
are mapped to their build-distro package with ``dpkg -S`` and their
``/usr/share/doc/<package>/copyright`` is copied. The result is a
``THIRD-PARTY-NOTICES.txt`` and one folder per component.
"""

import argparse
import ast
import fnmatch
import platform
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib.metadata import distributions
from pathlib import Path

BUILD_ONLY = {
    "pip",
    "wheel",
    "pyinstaller",
    "pyinstaller-hooks-contrib",
    "altgraph",
    "nadare",
    "nadare-launcher",
}

#: Code a wheel links into its extension without shipping the texts (see
#: ``packaging/third-party-licences/README.md``): component, licence, file in that folder.
STATIC_LINKED: dict[str, dict] = {
    "curl_cffi": {
        "checked": "0.16.3",
        "components": (
            ("curl 8.21.0 (curl-impersonate build)", "curl", "curl-COPYING"),
            ("curl-impersonate", "MIT", "curl-impersonate-LICENSE"),
            ("BoringSSL", "Apache-2.0 AND ISC AND OpenSSL", "boringssl-LICENSE"),
            ("libidn2", "LGPL-3.0-or-later OR GPL-2.0-or-later", "libidn2-COPYING.LESSERv3"),
            ("libidn2 (GPL option)", "GPL-2.0-or-later", "libidn2-COPYINGv2"),
            ("nghttp2", "MIT", "nghttp2-COPYING"),
            ("nghttp3", "MIT", "nghttp3-COPYING"),
            ("ngtcp2", "MIT", "ngtcp2-COPYING"),
            ("zstd", "BSD-3-Clause", "zstd-LICENSE"),
            ("brotli", "MIT", "brotli-LICENSE"),
            ("zlib", "Zlib", "zlib-LICENSE"),
        ),
        "source": (
            "Sources: https://github.com/lexiforest/curl-impersonate (the build) and "
            "https://gitlab.com/libidn/libidn2 (LGPL; the extension can be replaced by "
            "reinstalling curl_cffi from source)."
        ),
    },
    # The Qt wheel carries ICU's shared libraries next to Qt's (#1227 F4: they had no notice).
    "PyQt6-Qt6": {
        "checked": "6.11.2",
        "components": (
            (
                "ICU 73 (libicuuc, libicui18n, libicudata, in PyQt6/Qt6/lib)",
                "Unicode-3.0 AND ICU",
                "icu-LICENSE",
            ),
        ),
        # Library file prefixes these components cover (see unnoticed_wheel_libraries).
        "libraries": ("libicu",),
        "source": "Source: https://github.com/unicode-org/icu/releases/tag/release-73-2",
    },
}
STATIC_DIR = Path(__file__).resolve().parent.parent / "packaging" / "third-party-licences"


def unnoticed_wheel_libraries(internal: Path) -> list[str]:
    """Libraries the Qt wheel ships next to Qt's own that no notice names (#1227 F4: ICU).

    Qt's ``libQt6*`` are covered by the Qt section; anything else needs a STATIC_LINKED
    ``libraries`` prefix, or the strict build fails.
    """
    lib = internal / "PyQt6" / "Qt6" / "lib"
    if not lib.is_dir():
        return []
    covered = tuple(STATIC_LINKED.get("PyQt6-Qt6", {}).get("libraries", ()))
    return sorted(
        path.name
        for path in lib.iterdir()
        if ".so" in path.name
        and not path.name.startswith("libQt6")
        and not path.name.startswith(covered)
    )

_LICENCE_FILE_PATTERNS = ("licen[cs]e*", "copying*", "notice*")


@dataclass
class Entry:
    name: str
    version: str = ""
    licence: str = "unknown"
    files: list[str] = field(default_factory=list)
    libraries: list[str] = field(default_factory=list)
    missing: bool = False


def installed_distributions() -> list:
    """The installed distributions (a function so tests can monkeypatch it)."""
    return list(distributions())


def _normalise(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def _dist_info(dist) -> Path:
    """The ``.dist-info`` folder of a distribution."""
    path = getattr(dist, "_path", None)
    if path is not None:
        return Path(path)
    return Path(dist.locate_file(""))


def _licence_text(dist) -> str:
    meta = dist.metadata
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression

    licence = (meta.get("License") or "").strip()
    if licence and len(licence) < 100 and "\n" not in licence:
        return licence

    classifiers = [
        value.strip()
        for value in (meta.get_all("Classifier") or [])
        if value.strip().startswith("License ::")
    ]
    if classifiers:
        cleaned = []
        for value in classifiers:
            value = value.removeprefix("License :: OSI Approved :: ")
            value = value.removeprefix("License :: ")
            cleaned.append(value)
        return "; ".join(cleaned)

    return "unknown"


def _safe_relative(value: str) -> Path | None:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
    except (OSError, ValueError):
        return False
    return True


def _copy_licence(source: Path, destination: Path) -> bool:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    except OSError:
        return False
    return True


def _licence_files(dist, info: Path, destination: Path) -> list[str]:
    copied: list[str] = []

    for value in dist.metadata.get_all("License-File") or []:
        value = value.strip()
        relative = _safe_relative(value)
        if relative is None:
            continue
        source = None
        for candidate in (info / "licenses" / relative, info / relative):
            if candidate.is_file() and _inside(candidate, info):
                source = candidate
                break
        if source is None:
            continue
        target = destination / relative
        if _inside(target, destination) and _copy_licence(source, target):
            copied.append(relative.as_posix())

    if copied:
        return copied

    # Older packages list no License-File: take the conventional files from the
    # dist-info folder instead.
    for candidate in sorted(info.rglob("*")):
        if not candidate.is_file() or not _inside(candidate, info):
            continue
        if not any(
            fnmatch.fnmatch(candidate.name.lower(), pattern)
            for pattern in _LICENCE_FILE_PATTERNS
        ):
            continue
        relative = candidate.relative_to(info)
        target = destination / relative
        if _inside(target, destination) and _copy_licence(candidate, target):
            copied.append(relative.as_posix())

    return copied


def bundled_modules(toc: Path, internal: Path) -> set[str]:
    """Top-level module names PyInstaller put in the bundle.

    Pure-Python modules live inside the executable's archive, so they are read from
    PyInstaller's ``PYZ-00.toc``; extension modules and data folders are on disk in
    ``_internal``.
    """
    names: set[str] = set()
    try:
        _archive, entries = ast.literal_eval(toc.read_text(encoding="utf-8"))
    except (OSError, ValueError, SyntaxError, TypeError):
        entries = []
    for entry in entries:
        if isinstance(entry, tuple) and entry and isinstance(entry[0], str):
            names.add(entry[0].split(".", 1)[0])
    if internal.is_dir():
        for path in internal.iterdir():
            names.add(path.name.split(".", 1)[0])
    return names


def _top_level(dist) -> set[str]:
    """The top-level module names a distribution installs."""
    text = dist.read_text("top_level.txt")
    if text:
        return {line.strip() for line in text.splitlines() if line.strip()}
    names: set[str] = set()
    for file in dist.files or []:
        first = file.parts[0] if file.parts else ""
        if not first or first == ".." or first.endswith((".dist-info", ".data")):
            continue
        if first == "__pycache__":
            continue
        names.add(first.split(".", 1)[0])
    return names


def python_entries(
    dists: Iterable, out: Path, *, bundled: set[str] | None = None
) -> list[Entry]:
    """One entry per third-party distribution; with ``bundled``, only the ones in the bundle."""
    entries: list[Entry] = []
    seen: set[str] = set()

    for dist in dists:
        name = (dist.metadata.get("Name") or "").strip()
        key = _normalise(name)
        if not name or key in BUILD_ONLY or key in seen:
            continue
        if bundled is not None and not (_top_level(dist) & bundled):
            continue
        seen.add(key)

        info = _dist_info(dist)
        destination = out / "python" / name
        own = _licence_files(dist, info, destination)
        version = (dist.metadata.get("Version") or "").strip()
        entries.append(
            Entry(
                name=name,
                version=version,
                licence=_licence_text(dist),
                files=own + _static_files(name, version, destination),
                missing=not own,
            )
        )

    entries.sort(key=lambda entry: entry.name)
    return entries


def _static_files(name: str, version: str, destination: Path) -> list[str]:
    """Copy the texts of what ``name``'s extension links in statically into ``static/``."""
    info = STATIC_LINKED.get(name)
    if info is None:
        return []
    if version != info["checked"]:
        print(
            f"warning: {name} {version} differs from the checked {info['checked']}; "
            "re-check what its extension links in (packaging/third-party-licences/README.md)",
            file=sys.stderr,
        )
    copied: list[str] = []
    for _component, _licence, filename in info["components"]:
        source = STATIC_DIR / name / filename
        if source.is_file() and _copy_licence(source, destination / "static" / filename):
            copied.append(f"static/{filename}")
    return copied


def _bundled_libraries(internal: Path) -> list[str]:
    names: set[str] = set()
    for folder in (internal, internal / "fallback-libs"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if not fnmatch.fnmatch(path.name, "*.so*"):
                continue
            if not path.is_file() or path.is_symlink():
                continue
            name = path.name
            if name.startswith("libpython3"):
                continue
            if ".cpython-" in name or name.startswith("_"):
                continue
            names.add(name)
    return sorted(names)


def system_entries(
    internal: Path,
    out: Path,
    *,
    owner_of,
    doc_root: Path = Path("/usr/share/doc"),
) -> list[Entry]:
    grouped: dict[str, list[str]] = {}
    unknown: list[Entry] = []

    for name in _bundled_libraries(internal):
        owner = owner_of(name)
        if owner:
            grouped.setdefault(str(owner), []).append(name)
        else:
            unknown.append(
                Entry(name=name, licence="unknown", libraries=[name], missing=True)
            )

    entries: list[Entry] = []
    for package, libraries in grouped.items():
        source = doc_root / package / "copyright"
        destination = out / "system" / package / "copyright"
        files: list[str] = []
        if source.is_file() and _copy_licence(source, destination):
            files = ["copyright"]
        entries.append(
            Entry(
                name=package,
                licence="see copyright",
                files=files,
                libraries=sorted(libraries),
                missing=not files,
            )
        )

    entries.extend(unknown)
    entries.sort(key=lambda entry: entry.name)
    return entries


#: Nadare's own bundled third-party files: (name, licence, path under ``_internal``).
ASSETS = (
    ("7-Zip", "GNU LGPL and BSD; built from source without the RAR code (see the text)",
     "nadare/assets/licenses/7zip-License.txt"),
    ("Inter font", "OFL-1.1", "nadare/assets/fonts/OFL.txt"),
)


def asset_entries(internal: Path, out: Path) -> list[Entry]:
    """The 7-Zip binary and the Inter font ship inside Nadare's package with their texts."""
    entries: list[Entry] = []
    for name, licence, relative in ASSETS:
        source = internal / relative
        target = out / "other" / name / source.name
        files = [source.name] if source.is_file() and _copy_licence(source, target) else []
        entries.append(Entry(name=name, licence=licence, files=files, missing=not files))
    return entries


def dpkg_owner(name: str) -> str | None:
    """The package owning ``*/<name>`` according to dpkg, without the arch suffix."""
    try:
        done = subprocess.run(
            ["dpkg", "-S", f"*/{name}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        package, separator, _path = line.partition(": ")
        if separator and package:
            return package.rsplit(":", 1)[0]
    return None


def _cpython_entry(out: Path) -> Entry:
    version_info = sys.version_info
    source = (
        Path(sys.base_prefix)
        / "lib"
        / f"python{version_info.major}.{version_info.minor}"
        / "LICENSE.txt"
    )
    destination = out / "python" / "CPython" / "LICENSE.txt"
    files: list[str] = []
    if source.is_file() and _copy_licence(source, destination):
        files = ["LICENSE.txt"]
    return Entry(
        name="CPython",
        version=platform.python_version(),
        licence="PSF-2.0",
        files=files,
        missing=not files,
    )


def _missing_note(entry: Entry) -> str:
    return "  (no licence file in the package)" if entry.missing else ""


def write_notice(
    out: Path, python: list[Entry], system: list[Entry], other: list[Entry] | None = None
) -> Path:
    lines = [
        "Nadare is free software, licensed under the GNU General Public License",
        "version 3 only (GPL-3.0-only).",
        "",
        "Nadare bundles the components below. Each remains under its own licence;",
        "the licence texts are in the python/, system/ and other/ folders next to this file.",
        "",
        "Python components",
        "-----------------",
    ]
    for entry in python:
        lines.append(
            f"{entry.name} {entry.version}: {entry.licence}{_missing_note(entry)}"
        )
        static = STATIC_LINKED.get(entry.name)
        if static is not None:
            lines.append("  its extension includes (statically linked; texts in static/):")
            for component, licence, _filename in static["components"]:
                lines.append(f"    {component}: {licence}")
            lines.append(f"    {static['source']}")

    lines.extend(["", "System libraries", "----------------"])
    if system:
        for entry in system:
            lines.append(
                f"{entry.name}: {', '.join(entry.libraries)}{_missing_note(entry)}"
            )
    else:
        lines.append("(none bundled)")

    if other:
        lines.extend(["", "Other bundled files", "-------------------"])
        for entry in other:
            lines.append(f"{entry.name}: {entry.licence}{_missing_note(entry)}")

    qt = next((entry for entry in python if entry.name == "PyQt6-Qt6"), None)
    if qt is not None:
        version = qt.version or "unknown"
        parts = version.split(".")
        branch = ".".join(parts[:2]) if len(parts) >= 2 else version
        source_url = (
            f"https://download.qt.io/official_releases/qt/{branch}/{version}/"
        )
        archive_url = f"https://download.qt.io/archive/qt/{branch}/{version}/"
        lines.extend(
            [
                "",
                "Qt (LGPL-3.0)",
                "-------------",
                f"Qt {version} is used under the GNU Lesser General Public License v3.",
                f"The Qt source is available at {source_url}",
                f"(older releases: {archive_url}).",
                "",
                "The Qt libraries are separate shared files in",
                "usr/bin/_internal/PyQt6/Qt6/lib inside the AppImage; extract them",
                "with --appimage-extract. You may replace them with your own build",
                "of the same Qt version. PyQt6 itself is GPL-3.0-only, which is",
                "why Nadare as a whole is GPL-3.0-only.",
            ]
        )

    lines.append("")
    path = out / "THIRD-PARTY-NOTICES.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Collect third-party licence texts for the AppImage"
    )
    parser.add_argument("--internal", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--toc",
        type=Path,
        help="PyInstaller's PYZ-00.toc: list only the distributions in the bundle",
    )
    parser.add_argument(
        "--no-system", action="store_true", help="skip the bundled system libraries"
    )
    parser.add_argument(
        "--strict", action="store_true", help="fail when a licence text is missing"
    )
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)

    bundled = bundled_modules(args.toc, args.internal) if args.toc else None
    python = python_entries(installed_distributions(), args.out, bundled=bundled)
    python.append(_cpython_entry(args.out))
    python.sort(key=lambda entry: entry.name)

    system: list[Entry] = []
    if not args.no_system:
        system = system_entries(args.internal, args.out, owner_of=dpkg_owner)

    other = asset_entries(args.internal, args.out)
    write_notice(args.out, python, system, other)

    missing = [entry for entry in python + system + other if entry.missing]
    unnoticed = unnoticed_wheel_libraries(args.internal)
    for name in unnoticed:
        print(f"no notice for {name} (bundled with the Qt wheel)", file=sys.stderr)
    print(
        f"collected licences: {len(python)} python, {len(system)} system, "
        f"{len(missing)} without a licence file -> {args.out}"
    )
    for entry in missing:
        print(f"warning: no licence file for {entry.name}", file=sys.stderr)

    return 1 if args.strict and (missing or unnoticed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
