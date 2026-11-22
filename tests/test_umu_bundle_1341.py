# SPDX-License-Identifier: GPL-3.0-or-later
"""The frozen build carries every module the bundled umu zipapp has (#1341 item 3).

Read from the archive, not a hand-written list: umu updates would otherwise silently leave a
module out and the hidden ``--umu-run`` would fail only on the machine without python3.
"""

from __future__ import annotations

import importlib.util
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "packaging" / "umu_bundle.py"
SPEC = ROOT / "packaging" / "nadare.spec"
BUNDLED = ROOT / "src" / "nadare" / "assets" / "umu-launcher-1.4.4-zipapp.tar"


def _load_helper():
    spec = importlib.util.spec_from_file_location("umu_bundle_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _zipapp(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _archive(path: Path, payload: bytes) -> Path:
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("umu/umu-run")
        info.size = len(payload)
        info.mode = 0o755
        tar.addfile(info, io.BytesIO(payload))
    return path


def test_module_names_come_from_the_zipapp(tmp_path):
    payload = _zipapp(
        {
            "__main__.py": b"",
            "pkg/__init__.py": b"",
            "pkg/mod.py": b"",
            "single.py": b"",
            "pkg/data.txt": b"",
            "pkg-1.0.dist-info/METADATA": b"",
        }
    )
    archive = _archive(tmp_path / "umu.tar", payload)
    assert _load_helper().modules(archive) == ["_nadare_umu_entry", "pkg", "pkg.mod", "single"]


def test_extract_unpacks_the_zipapp_for_pyinstaller(tmp_path):
    payload = _zipapp({"__main__.py": b"", "pkg/__init__.py": b"", "pkg/mod.py": b""})
    archive = _archive(tmp_path / "umu.tar", payload)
    target = tmp_path / "unpacked"
    got = _load_helper().extract(archive, target)
    assert Path(got) == target
    assert (target / "__main__.py").is_file()
    assert (target / "pkg" / "mod.py").is_file()


def test_the_pyinstaller_spec_adds_the_umu_modules():
    spec = SPEC.read_text(encoding="utf-8")
    assert "umu_bundle" in spec
    assert "umu_hiddenimports = _umu_bundle.modules(_umu_archive)" in spec
    assert "hiddenimports=hiddenimports + umu_hiddenimports" in spec
    assert "pathex.append" in spec and "pathex=pathex" in spec


@pytest.mark.skipif(not BUNDLED.is_file(), reason="the umu archive is fetched at build time")
def test_the_bundled_zipapp_is_covered():
    modules = _load_helper().modules(BUNDLED)
    assert "umu.umu_run" in modules
    assert "__main__" not in modules
    assert "_nadare_umu_entry" in modules


def test_module_list_includes_entry_imports_and_nested_main(tmp_path):
    """PyInstaller also needs imports made only by the zipapp entry script."""
    archive = _archive(
        tmp_path / "umu.tar",
        _zipapp({
            "__main__.py": b"import fractions\nfrom xml.etree import ElementTree\n",
            "pkg/__init__.py": b"",
            "pkg/__main__.py": b"from decimal import Decimal\n",
        }),
    )
    helper = _load_helper()
    modules = helper.modules(archive)
    assert {"_nadare_umu_entry", "pkg", "pkg.__main__"} <= set(modules)
    assert "__main__" not in modules
    tree = helper.extract(archive, tmp_path / "tree")
    # The renamed entry is analyzed as a hidden import, including its from-imports.
    assert (tree / "_nadare_umu_entry.py").read_bytes() == (
        b"import fractions\nfrom xml.etree import ElementTree\n"
    )
    assert (tree / "pkg" / "__main__.py").read_bytes() == b"from decimal import Decimal\n"


def _execute_spec(tmp_path, monkeypatch):
    """Execute build wiring with fake PyInstaller hooks, without building the application."""
    import sys
    from types import ModuleType

    for name in ("PyInstaller", "PyInstaller.utils", "PyInstaller.utils.hooks"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    hooks = sys.modules["PyInstaller.utils.hooks"]
    hooks.collect_submodules = lambda name: []
    hooks.collect_dynamic_libs = lambda name: []
    packaging = tmp_path / "packaging"
    packaging.mkdir(exist_ok=True)
    (packaging / "umu_bundle.py").write_bytes(HELPER.read_bytes())
    analysis = type("AnalysisResult", (), {"pure": [], "scripts": [], "binaries": [], "datas": []})
    calls = []

    def analyze(*args, **kwargs):
        calls.append(kwargs)
        return analysis()

    scope = {
        "SPECPATH": str(packaging), "Analysis": analyze,
        "PYZ": lambda *a, **k: None, "EXE": lambda *a, **k: None,
        "COLLECT": lambda *a, **k: None,
    }
    exec(compile(SPEC.read_text(), str(SPEC), "exec"), scope)
    return calls[0]


@pytest.mark.parametrize("count", [0, 2])
def test_spec_refuses_missing_or_ambiguous_umu_archives(tmp_path, monkeypatch, count):
    assets = tmp_path / "src" / "nadare" / "assets"
    assets.mkdir(parents=True)
    for version in range(count):
        _archive(assets / f"umu-launcher-{version}-zipapp.tar", _zipapp({"__main__.py": b""}))
    with pytest.raises(SystemExit, match="umu"):
        _execute_spec(tmp_path, monkeypatch)


def test_spec_passes_archive_modules_to_analysis(tmp_path, monkeypatch):
    assets = tmp_path / "src" / "nadare" / "assets"
    assets.mkdir(parents=True)
    _archive(
        assets / "umu-launcher-1.4.4-zipapp.tar",
        _zipapp({"__main__.py": b"import fractions\n", "umu/__init__.py": b""}),
    )
    analysis = _execute_spec(tmp_path, monkeypatch)
    assert {"_nadare_umu_entry", "umu"} <= set(analysis["hiddenimports"])
    extracted = Path(analysis["pathex"][-1])
    assert (extracted / "_nadare_umu_entry.py").read_bytes() == b"import fractions\n"
