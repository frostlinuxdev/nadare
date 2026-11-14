# SPDX-License-Identifier: GPL-3.0-or-later
"""umu's own folder in the Flatpak, and where Nadare looks for umu's runtime (VMs, 2026-11-14).

umu keeps its Steam Runtime in ``$HOME/.local/share/umu`` whatever ``XDG_DATA_HOME`` says. The
Flatpak lost home access in #1244 and had no grant for that folder, so on fresh Ubuntu, Mint and
Fedora VMs the runtime umu downloaded was not where its container looked ("different file inside
and outside sandbox", "Can't find source path /proc/self/fd/23"); the KDE test desktop only
worked because an older build with home access had left a runtime there. Lutris and Bottles grant
``xdg-data/umu:create`` for the same reason. Nadare itself looked only under ``XDG_DATA_HOME``,
which inside the Flatpak is the app's own data folder.
"""

from __future__ import annotations

from pathlib import Path

from nadare.core import umu, userns

MANIFEST = Path(__file__).resolve().parents[1] / "packaging" / "flatpak" / (
    "io.github.frostlinuxdev.Nadare.yml"
)


def _finish_args() -> list[str]:
    lines = MANIFEST.read_text(encoding="utf-8").splitlines()
    return [line.strip()[2:] for line in lines if line.strip().startswith("- --")]


def test_the_manifest_grants_umus_own_folder_only():
    args = _finish_args()
    assert "--filesystem=xdg-data/umu:create" in args
    assert "--filesystem=home" not in args and "--filesystem=xdg-data" not in args


def _srt(root: Path) -> Path:
    srt = root / "umu" / "steamrt4" / "pressure-vessel" / "libexec" / "steam-runtime-tools-0"
    srt.mkdir(parents=True)
    (srt / "srt-bwrap").write_text("")
    return srt / "srt-bwrap"


def test_the_runtime_is_found_where_umu_puts_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".var" / "app" / "x" / "data"))
    assert umu.runtime_downloaded() is False
    copy = _srt(home / ".local" / "share")
    assert umu.runtime_downloaded() is True
    assert str(copy) in userns._srt_bwrap_paths()


def test_a_copy_under_xdg_data_home_still_counts_once(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share") + "/")
    copy = _srt(home / ".local" / "share")
    assert userns._srt_bwrap_paths() == [str(copy)]
