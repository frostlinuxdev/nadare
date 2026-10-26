# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression tests for the L1 CLI, docs and packaging sweep findings."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from nadare import app
from nadare.backends.fgo import readiness
from nadare.core.backends import BackendError
from nadare.core.config import Config

REPO = Path(__file__).resolve().parents[1]

#: These pin the wording of the maintainer docs (README/TESTERS/TESTING). The public
#: tree has its own shorter docs and no TESTING.md, so they skip there.
maintainer_docs = pytest.mark.skipif(
    not (REPO / "TESTING.md").exists(), reason="maintainer docs are not in the public tree"
)


def _config(tmp_path: Path, *profiles: tuple[str, dict]) -> tuple[Config, Path]:
    path = tmp_path / "config.json"
    cfg = Config.load(path)
    for name, values in profiles:
        cfg.add_profile(name, **values)
    cfg.save()
    return cfg, path


class _Backend:
    def __init__(self, *, launch_error: str | None = None, verify=None) -> None:
        self._launch_error = launch_error
        self._verify = verify

    def requires_notice_ack(self) -> bool:
        return False

    def launch(self) -> int:
        if self._launch_error:
            raise BackendError(self._launch_error, 1)
        return 0

    def verify(self):
        if isinstance(self._verify, BaseException):
            raise self._verify
        return self._verify


# ---------------------------------------------------------------------------
# #337 F-02: launch/verify on an uninstalled game name the install command
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["launch", "verify"])
def test_uninstalled_game_names_the_install_command(
    command, tmp_path, monkeypatch, capsys
) -> None:
    _, path = _config(tmp_path, ("game", {"backend": "wine"}))
    backend = _Backend(
        launch_error="the game is not installed",
        verify=BackendError("the game is not installed", 1),
    )
    monkeypatch.setattr(app, "create_backend", lambda *a, **k: backend)

    assert app.run_cli(["-c", str(path), command, "-p", "game"]) == 1

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Run: nadare install -p game" in output


# ---------------------------------------------------------------------------
# #337 F-04: a successful verify reports its result, not "nothing to verify"
# ---------------------------------------------------------------------------
def test_successful_verify_without_a_message_reports_success(
    tmp_path, monkeypatch, capsys
) -> None:
    _, path = _config(tmp_path, ("game", {"backend": "script"}))
    monkeypatch.setattr(
        app,
        "create_backend",
        lambda *a, **k: _Backend(verify={"verified": True, "script": "/tmp/run.sh"}),
    )

    assert app.run_cli(["-c", str(path), "verify", "-p", "game"]) == 0

    out = capsys.readouterr().out
    assert "nothing to verify" not in out
    assert "all checks passed" in out


# ---------------------------------------------------------------------------
# #337 F-06: -q / -v are accepted before and after every command
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ["-q", "list"],
        ["list", "-q"],
        ["-v", "doctor"],
        ["doctor", "-v"],
        ["-q", "install", "-p", "game"],
        ["install", "-p", "game", "-q"],
        ["-v", "fetch-art", "-p", "game"],
        ["fetch-art", "-p", "game", "-v"],
        ["-q", "shortcut", "add", "-p", "game"],
        ["shortcut", "-q", "add", "-p", "game"],
        ["shortcut", "add", "-p", "game", "-v"],
    ],
)
def test_quiet_and_verbose_are_accepted_by_every_command(argv) -> None:
    args = app.build_parser().parse_args(argv)
    assert args.quiet or args.verbose


# ---------------------------------------------------------------------------
# #337 F-07: list/doctor reject the profile flag they do not use
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("command", ["list", "doctor"])
def test_profile_is_rejected_where_it_is_ignored(command) -> None:
    with pytest.raises(SystemExit) as exc:
        app.build_parser().parse_args([command, "-p", "game"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# #337 F-10: shortcut add/remove without a profile explains what to pass
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("action", ["add", "remove"])
def test_shortcut_without_a_profile_does_not_print_a_blank_name(
    action, tmp_path, capsys
) -> None:
    _, path = _config(tmp_path)

    assert app.run_cli(["-c", str(path), "shortcut", action]) == 1

    err = capsys.readouterr().err
    assert "no profile given; use -p <profile>" in err
    assert "no such profile: \n" not in err


# ---------------------------------------------------------------------------
# #337 F-11: an explicit missing --config is an error, with exit 2
# ---------------------------------------------------------------------------
def test_missing_explicit_config_exits_two_with_its_path(tmp_path, capsys) -> None:
    missing = tmp_path / "missing.json"

    assert app.run_cli(["-c", str(missing), "list"]) == 2

    err = capsys.readouterr().err
    assert f"config file not found: {missing}" in err


# ---------------------------------------------------------------------------
# #341 F-12 / #340 F-07: tester guide matches the static AppImage runtime
# ---------------------------------------------------------------------------
@maintainer_docs
def test_tester_guide_and_testing_agree_about_fuse() -> None:
    testers = (REPO / "docs" / "TESTERS.md").read_text(encoding="utf-8")
    testing = (REPO / "TESTING.md").read_text(encoding="utf-8")

    assert "need FUSE 2" not in testers
    assert "libfuse2" in testers and "fusermount" in testers
    assert "libfuse2" in testing and "fusermount" in testing
    # #340 F-07: pages the artifacts the project actually publishes.
    assert "single x86_64 AppImage" in testers
    assert "no Flatpak or" in testers and "`.deb` release yet" in testers


# ---------------------------------------------------------------------------
# #341 F-13: README and TESTERS agree that AMD has been verified
# ---------------------------------------------------------------------------
@maintainer_docs
def test_readme_and_testers_agree_about_amd() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    testers = (REPO / "docs" / "TESTERS.md").read_text(encoding="utf-8")

    assert "AMD and Intel GPUs are not verified" not in readme
    assert "AMD (RX 7900 XTX" in readme
    assert "AMD (RX 7900 XTX" in testers


# ---------------------------------------------------------------------------
# #341 F-15: FGO prerequisites include zstd
# ---------------------------------------------------------------------------
@maintainer_docs
def test_fgo_prerequisites_include_zstd() -> None:
    readme = (REPO / "docs" / "SOURCE-INSTALL.md").read_text(encoding="utf-8")
    start = readme.index("**For Fate/Grand Order (NA)**")
    paragraph = readme[start : readme.index("## Install system packages", start)]

    assert "`zstd`" in paragraph
    testers = (REPO / "docs" / "TESTERS.md").read_text(encoding="utf-8")
    assert "zstd" in testers


# ---------------------------------------------------------------------------
# #341 F-16: the fetch-art example uses a profile key, not the display title
# ---------------------------------------------------------------------------
def test_fetch_art_example_uses_a_profile_key() -> None:
    artwork = (REPO / "docs" / "ARTWORK.md").read_text(encoding="utf-8")

    assert "fetch-art -p arknights" in artwork
    assert "fetch-art -p Arknights" not in artwork


# ---------------------------------------------------------------------------
# #340 F-06: the no-display hint names how to start this packaged build
# ---------------------------------------------------------------------------
def test_no_display_hint_names_the_appimage_command(monkeypatch) -> None:
    from nadare import gui

    for name in ("DISPLAY", "WAYLAND_DISPLAY", "QT_QPA_PLATFORM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gui.sandbox, "in_flatpak", lambda: False)
    monkeypatch.setenv("APPIMAGE", "/opt/Nadare-0.1.0b4-x86_64.AppImage")

    with pytest.raises(SystemExit) as exc:
        gui._require_display()

    assert "/opt/Nadare-0.1.0b4-x86_64.AppImage list" in str(exc.value)


# ---------------------------------------------------------------------------
# #340 F-08: the wheel ships the locale catalogue
# ---------------------------------------------------------------------------
def test_pyproject_ships_locale_package_data() -> None:
    with (REPO / "pyproject.toml").open("rb") as fh:
        data = tomllib.load(fh)

    assert "assets/locales/*.json" in data["tool"]["setuptools"]["package-data"]["nadare"]


# ---------------------------------------------------------------------------
# #336 F-06: doctor --fgo package-install fixes are run as root
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("family", ["debian", "fedora", "arch", "suse"])
def test_fgo_install_fixes_use_sudo(family) -> None:
    command = readiness.install_command(["podman", "zstd"], family)

    assert command.startswith("sudo ")
    assert "zstd" in command


# ---------------------------------------------------------------------------
# #336 F-10: the tester guide does not claim the whole project lacks FGO
# ---------------------------------------------------------------------------
@maintainer_docs
def test_tester_guide_fgo_wording_matches_the_packaged_builds() -> None:
    testers = (REPO / "docs" / "TESTERS.md").read_text(encoding="utf-8")

    assert "not** in the beta AppImage" in testers
    assert "Source checkouts" in testers
    assert "add-game wizard" in testers
    assert "podman, zstd" in testers


# ---------------------------------------------------------------------------
# #339 F-11: BACKENDS describes the Yostar generator accurately
# ---------------------------------------------------------------------------
def test_yostar_description_matches_the_generator() -> None:
    backends = (REPO / "docs" / "BACKENDS.md").read_text(encoding="utf-8")

    assert "signs the\ntwo launcher API calls" in backends
    assert "public game-file list" in backends
    assert "signs the\nthree launcher calls" not in backends
