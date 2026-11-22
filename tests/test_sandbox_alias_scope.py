# SPDX-License-Identifier: GPL-3.0-or-later
"""A linked path is shown at its written place only when its target is already in the Sandbox.

``alias`` puts a bound folder back at the path the game was told (``~/Games`` -> another disk).
It must never add something ``_bind_target`` refused: a link to the home folder would bring the
whole home back (found by an adversarial pass on b28).
"""

from __future__ import annotations

from pathlib import Path

from nadare.core import gamesandbox


def _binds(argv: list[str]) -> list[tuple[str, str, str]]:
    out = []
    for i, arg in enumerate(argv):
        if arg in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try"):
            out.append((arg, argv[i + 1], argv[i + 2]))
    return out


def _sources(argv: list[str]) -> list[Path]:
    return [Path(src).resolve() for _flag, src, _dst in _binds(argv)]


def _home_with_prefix(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("secret")
    prefix = tmp_path / "games" / "g1" / "prefix"
    prefix.mkdir(parents=True)
    return home, prefix


def test_an_xauthority_link_to_the_home_does_not_bring_the_home_back(tmp_path) -> None:
    home, prefix = _home_with_prefix(tmp_path)
    link = home / ".Xauthority"
    link.symlink_to(home)
    argv, _env = gamesandbox.wrap(
        ["/usr/bin/true"], {"XAUTHORITY": str(link)}, prefix=prefix, home=home
    )
    assert home.resolve() not in _sources(argv)


def test_a_umu_folder_linked_to_the_home_does_not_bring_the_home_back(tmp_path) -> None:
    home, prefix = _home_with_prefix(tmp_path)
    share = home / ".local" / "share"
    share.mkdir(parents=True)
    (share / "umu").symlink_to(home)
    argv, _env = gamesandbox.wrap(
        ["/usr/bin/true"], {"XDG_DATA_HOME": str(share)}, prefix=prefix, home=home
    )
    assert home.resolve() not in _sources(argv)


def test_a_linked_prefix_is_still_shown_at_its_written_path(tmp_path) -> None:
    home = tmp_path / "home"
    disk = tmp_path / "disk" / "prefix"
    disk.mkdir(parents=True)
    written = home / "Games" / "prefix"
    written.parent.mkdir(parents=True)
    written.symlink_to(disk)
    argv, _env = gamesandbox.wrap(["/usr/bin/true"], {}, prefix=written, home=home)
    assert ("--bind", str(disk), str(written)) in _binds(argv)


def test_a_linked_xauthority_file_is_still_shown_at_its_written_path(tmp_path) -> None:
    home, prefix = _home_with_prefix(tmp_path)
    runtime = tmp_path / "run"
    runtime.mkdir()
    cookie = runtime / "xauth_abc"
    cookie.write_text("cookie")
    link = home / ".Xauthority"
    link.symlink_to(cookie)
    argv, _env = gamesandbox.wrap(
        ["/usr/bin/true"], {"XAUTHORITY": str(link)}, prefix=prefix, home=home
    )
    assert ("--ro-bind", str(cookie), str(link)) in _binds(argv)
