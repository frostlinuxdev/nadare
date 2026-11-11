# SPDX-License-Identifier: GPL-3.0-or-later
"""#587: the AppImage carries the licence texts of what it bundles, plus the Qt LGPL notice.

``scripts/collect_licences.py`` runs at build time with the build interpreter. These tests feed
it fake installed distributions and a fake ``_internal`` folder, so nothing depends on the host.
"""

from __future__ import annotations

import importlib.util
from importlib.metadata import PathDistribution
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "collect_licences.py"


@pytest.fixture(scope="module")
def cl():
    spec = importlib.util.spec_from_file_location("collect_licences_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _dist(site: Path, name: str, version: str, *, licence: str | None = None,
          files: dict[str, str] | None = None, classifier: str | None = None) -> PathDistribution:
    """A minimal ``<name>-<version>.dist-info`` with optional licence files under ``licenses/``."""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    meta = ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}"]
    if licence:
        meta.append(f"License-Expression: {licence}")
    if classifier:
        meta.append(f"Classifier: {classifier}")
    for rel, text in (files or {}).items():
        meta.append(f"License-File: {rel}")
        target = info / "licenses" / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    (info / "METADATA").write_text("\n".join(meta) + "\n", encoding="utf-8")
    return PathDistribution(info)


# ---- Python distributions ------------------------------------------------------------------------
def test_each_bundled_distribution_gets_its_licence_files_copied(cl, tmp_path):
    site = tmp_path / "site"
    dists = [
        _dist(site, "certifi", "2026.1.1", licence="MPL-2.0", files={"LICENSE": "MPL text"}),
        _dist(site, "cryptography", "50.0.2", licence="Apache-2.0 OR BSD-3-Clause",
              files={"LICENSE.APACHE": "apache", "LICENSE.BSD": "bsd"}),
    ]
    out = tmp_path / "out"
    entries = cl.python_entries(dists, out)
    by_name = {e.name: e for e in entries}
    assert by_name["certifi"].licence == "MPL-2.0"
    assert by_name["certifi"].version == "2026.1.1"
    assert (out / "python" / "certifi" / "LICENSE").read_text() == "MPL text"
    assert (out / "python" / "cryptography" / "LICENSE.BSD").read_text() == "bsd"
    assert by_name["cryptography"].files and not by_name["cryptography"].missing


def test_build_only_tools_are_left_out(cl, tmp_path):
    site = tmp_path / "site"
    dists = [_dist(site, name, "1.0", licence="MIT", files={"LICENSE": "x"})
             for name in ("pip", "pyinstaller", "pyinstaller-hooks-contrib", "altgraph", "nadare",
                          "keyring")]
    names = {e.name for e in cl.python_entries(dists, tmp_path / "out")}
    assert names == {"keyring"}


def test_the_licence_falls_back_to_the_classifier(cl, tmp_path):
    dist = _dist(tmp_path / "site", "zstandard", "0.25.0", files={"LICENSE": "bsd"},
                 classifier="License :: OSI Approved :: BSD License")
    (entry,) = cl.python_entries([dist], tmp_path / "out")
    assert "BSD" in entry.licence


def test_a_distribution_without_a_licence_file_is_reported_not_skipped(cl, tmp_path):
    dist = _dist(tmp_path / "site", "mystery", "1.0", licence="MIT")
    (entry,) = cl.python_entries([dist], tmp_path / "out")
    assert entry.missing and entry.files == []


def test_licence_file_names_cannot_escape_the_output_folder(cl, tmp_path):
    site = tmp_path / "site"
    dist = _dist(site, "evil", "1.0", licence="MIT", files={"LICENSE": "ok"})
    meta = Path(dist._path) / "METADATA"
    meta.write_text(meta.read_text() + "License-File: ../../../escaped\n", encoding="utf-8")
    cl.python_entries([dist], tmp_path / "out")
    assert not (tmp_path / "escaped").exists()
    assert not any("escaped" in p.name for p in tmp_path.rglob("*") if "out" not in p.parts)



def _with_module(dist: PathDistribution, top: str) -> PathDistribution:
    (Path(dist._path) / "top_level.txt").write_text(top + "\n", encoding="utf-8")
    return dist


def test_only_distributions_pyinstaller_bundled_are_listed(cl, tmp_path):
    site = tmp_path / "site"
    dists = [
        _with_module(_dist(site, "keyring", "25.0", licence="MIT", files={"LICENSE": "k"}),
                     "keyring"),
        _with_module(_dist(site, "pytest", "9.0", licence="MIT", files={"LICENSE": "p"}),
                     "pytest"),
        _with_module(_dist(site, "zstandard", "0.25", licence="BSD-3-Clause",
                           files={"LICENSE": "z"}), "zstandard"),
        _with_module(_dist(site, "setuptools", "80", licence="MIT", files={"LICENSE": "s"}),
                     "setuptools"),
    ]
    toc = tmp_path / "PYZ-00.toc"
    toc.write_text(repr(("x.pyz", [
        ("keyring", "/v/keyring/__init__.py", "PYMODULE"),
        ("keyring.backends", "/v/keyring/backends/__init__.py", "PYMODULE"),
        ("json", "/py/json/__init__.py", "PYMODULE"),
    ])), encoding="utf-8")
    internal = _internal(tmp_path, "zstandard/backend_c.cpython-312-x86_64-linux-gnu.so")
    bundled = cl.bundled_modules(toc, internal)
    assert {"keyring", "json", "zstandard"} <= bundled
    names = {e.name for e in cl.python_entries(dists, tmp_path / "out", bundled=bundled)}
    assert names == {"keyring", "zstandard"}


def test_nadare_itself_is_not_a_third_party_component(cl, tmp_path):
    dist = _dist(tmp_path / "site", "nadare-launcher", "0.1.0b22", licence="GPL-3.0-or-later",
                 files={"LICENSE": "gpl"})
    assert cl.python_entries([dist], tmp_path / "out") == []


def test_cpython_is_named_by_its_plain_version(cl, tmp_path):
    import platform

    entry = cl._cpython_entry(tmp_path / "out")
    assert entry.version == platform.python_version()


# ---- bundled system libraries --------------------------------------------------------------------
def _internal(tmp_path: Path, *names: str) -> Path:
    internal = tmp_path / "_internal"
    (internal / "fallback-libs").mkdir(parents=True)
    for name in names:
        target = internal / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x7fELF")
    return internal


def test_system_libraries_map_to_their_package_copyright(cl, tmp_path):
    internal = _internal(tmp_path, "libffi.so.8", "fallback-libs/libfreetype.so.6",
                         "libpython3.12.so.1.0", "PyQt6/Qt6/lib/libQt6Core.so.6",
                         "_cffi_backend.cpython-312-x86_64-linux-gnu.so")
    docs = tmp_path / "doc"
    for pkg in ("libffi8", "libfreetype6"):
        (docs / pkg).mkdir(parents=True)
        (docs / pkg / "copyright").write_text(f"{pkg} copyright", encoding="utf-8")
    owners = {"libffi.so.8": "libffi8", "libfreetype.so.6": "libfreetype6"}
    out = tmp_path / "out"
    entries = cl.system_entries(internal, out, owner_of=owners.get, doc_root=docs)
    by_pkg = {e.name: e for e in entries}
    assert set(by_pkg) == {"libffi8", "libfreetype6"}
    assert (out / "system" / "libffi8" / "copyright").read_text() == "libffi8 copyright"
    assert by_pkg["libfreetype6"].libraries == ["libfreetype.so.6"]


def test_an_unknown_system_library_is_reported(cl, tmp_path):
    internal = _internal(tmp_path, "libmystery.so.1")
    entries = cl.system_entries(internal, tmp_path / "out", owner_of=lambda name: None,
                                doc_root=tmp_path / "doc")
    (entry,) = entries
    assert entry.missing and entry.libraries == ["libmystery.so.1"]



def test_nadares_own_bundled_programs_and_fonts_are_listed(cl, tmp_path):
    internal = _internal(tmp_path)
    assets = internal / "nadare" / "assets"
    (assets / "licenses").mkdir(parents=True)
    (assets / "licenses" / "7zip-License.txt").write_text("7z", encoding="utf-8")
    (assets / "fonts").mkdir(parents=True)
    (assets / "fonts" / "OFL.txt").write_text("ofl", encoding="utf-8")
    entries = cl.asset_entries(internal, tmp_path / "out")
    assert {e.name for e in entries} == {"7-Zip", "Inter font"}
    assert (tmp_path / "out" / "other" / "7-Zip" / "7zip-License.txt").read_text() == "7z"
    assert (tmp_path / "out" / "other" / "Inter font" / "OFL.txt").read_text() == "ofl"
    text = cl.write_notice(tmp_path / "out", [], [], entries).read_text(encoding="utf-8")
    assert "7-Zip" in text and "Inter font" in text and "OFL-1.1" in text


# ---- the notice file -----------------------------------------------------------------------------
def test_the_notice_names_every_component_and_the_qt_lgpl_terms(cl, tmp_path):
    site = tmp_path / "site"
    out = tmp_path / "out"
    py = cl.python_entries([
        _dist(site, "PyQt6-Qt6", "6.10.1", licence="LGPL-3.0-only", files={"LICENSE": "lgpl"}),
        _dist(site, "PyQt6", "6.10.0", licence="GPL-3.0-only", files={"LICENSE": "gpl"}),
        _dist(site, "mystery", "1.0", licence="MIT"),
    ], out)
    internal = _internal(tmp_path, "libffi.so.8")
    sysent = cl.system_entries(internal, out, owner_of=lambda name: None, doc_root=tmp_path / "d")
    path = cl.write_notice(out, py, sysent)
    text = path.read_text(encoding="utf-8")
    assert path.name == "THIRD-PARTY-NOTICES.txt"
    for word in ("PyQt6-Qt6 6.10.1", "LGPL-3.0-only", "PyQt6 6.10.0", "GPL-3.0-only",
                 "mystery 1.0", "libffi.so.8"):
        assert word in text
    # LGPL: where the Qt source is, and that the Qt libraries can be swapped for your own build.
    assert "https://download.qt.io/" in text and "6.10" in text
    assert "PyQt6/Qt6/lib" in text and "replace" in text.lower()
    assert "no licence file" in text.lower()


def test_main_writes_into_the_output_folder_and_exits_zero(cl, tmp_path):
    internal = _internal(tmp_path)
    out = tmp_path / "out"
    assert cl.main(["--internal", str(internal), "--out", str(out), "--no-system"]) == 0
    assert (out / "THIRD-PARTY-NOTICES.txt").is_file()


def test_strict_fails_when_something_has_no_licence(cl, tmp_path, monkeypatch):
    dist = _dist(tmp_path / "site", "mystery", "1.0", licence="MIT")
    monkeypatch.setattr(cl, "installed_distributions", lambda: [dist])
    internal = _internal(tmp_path)
    args = ["--internal", str(internal), "--out", str(tmp_path / "out"), "--no-system"]
    assert cl.main(args + ["--strict"]) != 0
    assert cl.main(args) == 0


# ---- wiring --------------------------------------------------------------------------------------
def test_the_appimage_build_runs_it_with_the_build_interpreter():
    text = (ROOT / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
    assert 'scripts/collect_licences.py' in text
    line = next(ln for ln in text.splitlines() if "collect_licences.py" in ln)
    assert '"$PY"' in line and "usr/share/licenses/nadare" in text
    assert "--toc build/nadare/PYZ-00.toc" in line


def test_packaging_doc_no_longer_says_it_is_missing():
    text = (ROOT / "docs" / "PACKAGING.md").read_text(encoding="utf-8")
    assert "Not yet automated: third-party licence texts" not in text
    assert "THIRD-PARTY-NOTICES.txt" in text


# ---- #587: code a wheel links in statically ------------------------------------------------------
def test_statically_linked_components_are_added_to_their_wheel(cl, tmp_path):
    site = tmp_path / "site"
    dist = _dist(site, "curl_cffi", cl.STATIC_LINKED["curl_cffi"]["checked"], licence="MIT",
                 files={"LICENSE": "mit"})
    out = tmp_path / "out"
    (entry,) = cl.python_entries([dist], out)
    assert "static/curl-COPYING" in entry.files
    assert "static/boringssl-LICENSE" in entry.files
    assert (out / "python" / "curl_cffi" / "static" / "libidn2-COPYING.LESSERv3").is_file()
    text = cl.write_notice(out, [entry], []).read_text(encoding="utf-8")
    for word in ("curl", "BoringSSL", "libidn2", "LGPL-3.0-or-later", "nghttp2", "zlib",
                 "https://gitlab.com/libidn/libidn2"):
        assert word in text


def test_every_listed_static_text_is_in_the_repo(cl):
    for name, info in cl.STATIC_LINKED.items():
        folder = ROOT / "packaging" / "third-party-licences" / name
        for _component, _licence, filename in info["components"]:
            assert (folder / filename).is_file(), filename


def test_a_different_wheel_version_is_flagged(cl, tmp_path, capsys):
    dist = _dist(tmp_path / "site", "curl_cffi", "9.9.9", licence="MIT", files={"LICENSE": "m"})
    (entry,) = cl.python_entries([dist], tmp_path / "out")
    assert "static/curl-COPYING" in entry.files  # still shipped
    assert "9.9.9" in capsys.readouterr().err


def test_the_notice_states_nadares_real_licence(cl, tmp_path):
    """#1227 F1: the shipped notice said GPL-3.0-or-later; Nadare is GPL-3.0-only (PyQt6 is
    GPL-3.0-only, so the combined work cannot offer "or later")."""
    import tomllib

    site, out = tmp_path / "site", tmp_path / "out"
    py = cl.python_entries(
        [
            _dist(site, "PyQt6-Qt6", "6.11.2", licence="LGPL-3.0-only", files={"LICENSE": "x"}),
            _dist(site, "PyQt6", "6.11.0", licence="GPL-3.0-only", files={"LICENSE": "x"}),
        ],
        out,
    )
    text = cl.write_notice(out, py, []).read_text(encoding="utf-8")
    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    licence = project["license"]
    licence = licence["text"] if isinstance(licence, dict) else licence
    assert licence == "GPL-3.0-only"
    head = text.split("Python components", 1)[0]
    assert "GPL-3.0-only" in head and "or later" not in head and "or-later" not in head
    qt_part = text.split("Qt (LGPL-3.0)", 1)[1]
    assert "GPL-3.0-or-later" not in qt_part and "GPL-3.0-only" in qt_part


def test_icu_inside_the_qt_wheel_is_named_with_its_licence(cl, tmp_path):
    """#1227 F4: ICU (libicu*.so.73) ships inside the PyQt6-Qt6 wheel and had no notice."""
    site, out = tmp_path / "site", tmp_path / "out"
    py = cl.python_entries(
        [_dist(site, "PyQt6-Qt6", "6.11.2", licence="LGPL-3.0-only", files={"LICENSE": "x"})],
        out,
    )
    text = cl.write_notice(out, py, []).read_text(encoding="utf-8")
    assert "ICU" in text and "Unicode" in text
    copied = list(out.rglob("icu-LICENSE"))
    assert copied and "UNICODE" in copied[0].read_text(encoding="utf-8")


def test_a_non_qt_library_in_the_qt_wheel_without_a_notice_is_reported(cl, tmp_path):
    """The guard that would have caught ICU: libraries the Qt wheel ships next to Qt's own."""
    lib = tmp_path / "_internal" / "PyQt6" / "Qt6" / "lib"
    lib.mkdir(parents=True)
    for name in ("libQt6Core.so.6", "libicuuc.so.73", "libicudata.so.73", "libmystery.so.1"):
        (lib / name).write_bytes(b"\x7fELF")
    assert cl.unnoticed_wheel_libraries(tmp_path / "_internal") == ["libmystery.so.1"]


def test_strict_fails_on_an_unnoticed_wheel_library(cl, tmp_path, monkeypatch):
    lib = tmp_path / "_internal" / "PyQt6" / "Qt6" / "lib"
    lib.mkdir(parents=True)
    (lib / "libmystery.so.1").write_bytes(b"\x7fELF")
    monkeypatch.setattr(cl, "installed_distributions", lambda: [])
    monkeypatch.setattr(cl, "_cpython_entry", lambda out: cl.Entry("CPython", "3", "PSF-2.0"))
    monkeypatch.setattr(cl, "asset_entries", lambda internal, out: [])
    argv = ["--internal", str(tmp_path / "_internal"), "--out", str(tmp_path / "out"),
            "--no-system", "--strict"]
    assert cl.main(argv) == 1
    (lib / "libmystery.so.1").unlink()
    assert cl.main(argv) == 0
