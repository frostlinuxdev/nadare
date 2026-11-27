# SPDX-License-Identifier: GPL-3.0-or-later
"""build_repo.sh composes the store catalogue with PNG icons, and stops if it has none.

AppStream 1.1+ writes catalogue icons as JPEG XL unless told otherwise. flatpak's export and
build-bundle only pick up ``app-info/icons/flatpak/<size>/<id>.png``, so a repository built with
a new appstreamcli shipped no icon at all: GNOME Software showed a generic gear for the bundle.
AppStream 1.0 has no ``--image-format`` and already writes PNG.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

FLATPAK_DIR = Path(__file__).resolve().parents[1] / "packaging" / "flatpak"
APP_ID = "io.github.frostlinuxdev.Nadare"

_STUB = """#!/usr/bin/env bash
printf '%s' "$(basename "$0")" >> "$STUB_LOG"
for a in "$@"; do printf '\\t%s' "$a" >> "$STUB_LOG"; done
printf '\\n' >> "$STUB_LOG"
case "$(basename "$0") $1" in
  "gpg --export") printf 'KEY' ;;
  "flatpak build-bundle") printf 'BUNDLE' > "${@: -3:1}" ;;
  "appstreamcli compose")
    if [ "$2" = "--help" ]; then
      echo "  --icons-dir=DIR   Override the icon output directory."
      [ -n "${STUB_OLD_APPSTREAM:-}" ] || echo "  --image-format=FORMAT   jxl by default, or png"
      exit 0
    fi
    ext=jxl
    for a in "$@"; do
      case "$a" in
        --result-root=*) root="${a#--result-root=}" ;;
        --image-format=png) ext=png ;;
        --image-format=*) echo "unknown option $a" >&2; exit 1 ;;
      esac
    done
    [ -n "${STUB_OLD_APPSTREAM:-}" ] && ext=png
    for size in 64x64 128x128; do
      d="$root/share/swcatalog/icons/$STUB_APP/$size"; mkdir -p "$d"
      printf 'ICON' > "$d/$STUB_APP.$ext"
    done ;;
esac
exit 0
"""


def _env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("flatpak", "gpg", "appstreamcli"):
        stub = bin_dir / name
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(0o755)
    env = dict(os.environ)
    env.update(PATH=f"{bin_dir}:/usr/bin:/bin", STUB_LOG=str(tmp_path / "calls.log"),
               STUB_APP=APP_ID, NADARE_FLATPAK_URL="https://flatpak.example/",
               NADARE_FLATPAK_KEY="F2D5C5AE93F8E5581CBAAF9C8E33695135F1C7E1")
    env.pop("GNUPGHOME", None)
    return env


def _build(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    staged = tmp_path / "root"
    (staged / "files").mkdir(parents=True)
    (staged / "finish-argv.json").write_text("[]", encoding="utf-8")
    (staged / "metadata").write_text("[Application]\n[Context]\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    return subprocess.run(["bash", str(FLATPAK_DIR / "build_repo.sh"), str(staged), str(repo)],
                          env=env, capture_output=True, text=True, timeout=60)


def _compose_calls(env: dict[str, str]) -> list[list[str]]:
    lines = Path(env["STUB_LOG"]).read_text(encoding="utf-8").splitlines()
    calls = [line.split("\t") for line in lines]
    return [c for c in calls if c[:2] == ["appstreamcli", "compose"] and "--help" not in c]


def _icons(tmp_path: Path) -> list[Path]:
    base = tmp_path / "root" / "files" / "share" / "app-info" / "icons" / "flatpak"
    return sorted(base.glob("*/*")) if base.is_dir() else []


def test_a_new_appstreamcli_is_asked_for_png_icons_and_they_reach_the_export(tmp_path):
    env = _env(tmp_path)
    result = _build(tmp_path, env)
    assert result.returncode == 0, result.stderr
    (compose,) = _compose_calls(env)
    assert "--image-format=png" in compose
    assert [p.relative_to(p.parents[1]).as_posix() for p in _icons(tmp_path)] == [
        f"128x128/{APP_ID}.png", f"64x64/{APP_ID}.png"]


def test_an_appstreamcli_without_the_option_still_composes_png(tmp_path):
    env = _env(tmp_path)
    env["STUB_OLD_APPSTREAM"] = "1"
    result = _build(tmp_path, env)
    assert result.returncode == 0, result.stderr
    (compose,) = _compose_calls(env)
    assert not any(a.startswith("--image-format") for a in compose)
    assert len(_icons(tmp_path)) == 2


def test_a_catalogue_without_a_png_icon_stops_the_build_before_export(tmp_path):
    env = _env(tmp_path)
    # A compose that ignores --image-format and still writes JPEG XL.
    stub = tmp_path / "bin" / "appstreamcli"
    stub.write_text(_STUB.replace("--image-format=png) ext=png", "--image-format=png) :"),
                    encoding="utf-8")
    result = _build(tmp_path, env)
    assert result.returncode != 0
    assert "PNG" in result.stderr
    calls = Path(env["STUB_LOG"]).read_text(encoding="utf-8")
    assert "build-export" not in calls
