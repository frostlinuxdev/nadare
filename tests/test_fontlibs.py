# SPDX-License-Identifier: GPL-3.0-or-later
"""#669: the AppImage uses the host's fontconfig and freetype, its own copies only as a fallback.

The bundled fontconfig 2.13 (Ubuntu 22.04) cannot parse current ``/etc/fonts`` syntax and prints
a wall of errors on Arch/CachyOS; the host's own library understands its own config. Qt links
fontconfig directly, so a host without it must still get the bundled copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nadare.core import fontlibs

ELF64 = b"\x7fELF\x02" + b"\0" * 59
ELF32 = b"\x7fELF\x01" + b"\0" * 59


def _lib(folder: Path, name: str, data: bytes = ELF64) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    return path


@pytest.fixture
def layout(tmp_path):
    fallback = tmp_path / "_internal" / "fallback-libs"
    _lib(fallback, "libfreetype.so.6")
    _lib(fallback, "libfontconfig.so.1")
    host = tmp_path / "usr" / "lib"
    return fallback, host


def test_the_host_copies_are_loaded_first_freetype_before_fontconfig(layout):
    fallback, host = layout
    ft = _lib(host, "libfreetype.so.6")
    fc = _lib(host, "libfontconfig.so.1")
    loaded: list[str] = []
    used = fontlibs.prefer_host_font_libs(fallback, host_dirs=[host], load=loaded.append)
    assert loaded == [str(ft), str(fc)]
    assert used == {"libfreetype.so.6": "host", "libfontconfig.so.1": "host"}


def test_without_a_host_copy_the_bundled_one_is_used(layout):
    fallback, host = layout
    _lib(host, "libfreetype.so.6")
    loaded: list[str] = []
    used = fontlibs.prefer_host_font_libs(fallback, host_dirs=[host], load=loaded.append)
    assert loaded[-1] == str(fallback / "libfontconfig.so.1")
    assert used["libfontconfig.so.1"] == "bundled"


def test_a_host_copy_that_fails_to_load_falls_back(layout):
    fallback, host = layout
    _lib(host, "libfreetype.so.6")
    _lib(host, "libfontconfig.so.1")
    loaded: list[str] = []

    def load(path: str) -> None:
        if path.startswith(str(host)):
            raise OSError("version GLIBC_2.38 not found")
        loaded.append(path)

    used = fontlibs.prefer_host_font_libs(fallback, host_dirs=[host], load=load)
    assert loaded == [str(fallback / "libfreetype.so.6"), str(fallback / "libfontconfig.so.1")]
    assert set(used.values()) == {"bundled"}


def test_a_32_bit_host_copy_is_never_used(layout):
    fallback, host = layout
    _lib(host, "libfreetype.so.6", ELF32)
    _lib(host, "libfontconfig.so.1", ELF32)
    loaded: list[str] = []
    fontlibs.prefer_host_font_libs(fallback, host_dirs=[host], load=loaded.append)
    assert all(p.startswith(str(fallback)) for p in loaded)


def test_nothing_happens_outside_the_appimage(tmp_path):
    loaded: list[str] = []
    used = fontlibs.prefer_host_font_libs(
        tmp_path / "nope", host_dirs=[tmp_path], load=loaded.append
    )
    assert used == {} and loaded == []


def test_a_failure_everywhere_never_raises(layout):
    fallback, host = layout

    def load(path: str) -> None:
        raise OSError("broken")

    assert fontlibs.prefer_host_font_libs(fallback, host_dirs=[host], load=load) == {
        "libfreetype.so.6": "none",
        "libfontconfig.so.1": "none",
    }


def test_the_build_moves_the_bundled_copies_aside():
    script = Path("scripts/build_appimage.sh").read_text(encoding="utf-8")
    assert "fallback-libs" in script
    assert "libfontconfig.so.1" in script and "libfreetype.so.6" in script


def test_the_frozen_entry_prefers_host_font_libs_before_qt():
    entry = Path("packaging/nadare_entry.py").read_text(encoding="utf-8")
    main = entry.index("def main(")
    prefer = entry.index("prefer_host_font_libs_frozen()", main)
    assert prefer < entry.index("return _run_gui()", main)
    assert prefer < entry.index("runpy.run_module", main)
