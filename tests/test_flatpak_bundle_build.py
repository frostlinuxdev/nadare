# SPDX-License-Identifier: GPL-3.0-or-later
"""build_repo.sh writes the single-file bundle, and upload.sh publishes it last.

Bazaar (Bazzite's store) ignores the repository named in a .flatpakref (#1410); a bundle made
with ``--repo-url`` carries the repository and key, so updates still come from
flatpak.nadare.app. The bundle used to be a hand step after every release. These tests run the
real scripts with ``flatpak``, ``gpg``, ``appstreamcli`` and ``rclone`` replaced by stubs that
only record their arguments.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

FLATPAK_DIR = Path(__file__).resolve().parents[1] / "packaging" / "flatpak"
APP_ID = "io.github.frostlinuxdev.Nadare"
URL = "https://flatpak.example/"
KEY = "F2D5C5AE93F8E5581CBAAF9C8E33695135F1C7E1"

_STUB = """#!/usr/bin/env bash
printf '%s' "$(basename "$0")" >> "$STUB_LOG"
for a in "$@"; do printf '\\t%s' "$a" >> "$STUB_LOG"; done
printf '\\n' >> "$STUB_LOG"
case "$(basename "$0") $1" in
  "gpg --export") printf 'PUBLICKEYBYTES' ;;
  "flatpak build-bundle")
    for a in "$@"; do case "$a" in --gpg-keys=*) cp "${a#--gpg-keys=}" "$STUB_KEYCOPY" ;; esac; done
    out="${@: -3:1}"; printf 'BUNDLE' > "$out" ;;
esac
if [ -n "${STUB_FAIL_BUNDLE:-}" ] && [ "$(basename "$0") $1" = "flatpak build-bundle" ]; then
  exit 1
fi
exit 0
"""


def _env(tmp_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("flatpak", "gpg", "appstreamcli", "rclone"):
        stub = bin_dir / name
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{bin_dir}:/usr/bin:/bin",
        STUB_LOG=str(tmp_path / "calls.log"),
        STUB_KEYCOPY=str(tmp_path / "key-given-to-bundle"),
        NADARE_FLATPAK_URL=URL,
        NADARE_FLATPAK_KEY=KEY,
        RCLONE=str(bin_dir / "rclone"),
    )
    env.pop("GNUPGHOME", None)
    return env


def _staged(tmp_path: Path) -> Path:
    staged = tmp_path / "root"
    (staged / "files").mkdir(parents=True)
    (staged / "finish-argv.json").write_text("[]", encoding="utf-8")
    (staged / "metadata").write_text("[Application]\n[Context]\n", encoding="utf-8")
    return staged


def _calls(env: dict[str, str]) -> list[list[str]]:
    lines = Path(env["STUB_LOG"]).read_text(encoding="utf-8").splitlines()
    return [line.split("\t") for line in lines]


def _build(tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(FLATPAK_DIR / "build_repo.sh"), str(_staged(tmp_path)), str(repo)],
        env=env, capture_output=True, text=True, timeout=60,
    )


def test_build_repo_writes_a_bundle_that_carries_the_repository_and_key(tmp_path):
    env = _env(tmp_path)
    result = _build(tmp_path, env)
    assert result.returncode == 0, result.stderr
    bundles = [c for c in _calls(env) if c[:2] == ["flatpak", "build-bundle"]]
    assert len(bundles) == 1
    args = bundles[0][2:]
    assert f"--repo-url={URL}" in args
    assert "--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo" in args
    assert any(a.startswith("--gpg-keys=") for a in args)
    # Positional: <repo> <file> <app id> <branch>, exactly.
    assert args[-4:] == [str(tmp_path / "repo"), str(tmp_path / "repo" / "nadare.flatpak"),
                         APP_ID, "master"]
    assert (tmp_path / "repo" / "nadare.flatpak").read_text() == "BUNDLE"
    # The key the bundle trusts is the signing key's export, not some other file.
    assert (tmp_path / "key-given-to-bundle").read_bytes() == b"PUBLICKEYBYTES"


def test_the_bundle_is_made_after_the_repository_summary_is_signed(tmp_path):
    env = _env(tmp_path)
    assert _build(tmp_path, env).returncode == 0
    verbs = [c[1] for c in _calls(env) if c[0] == "flatpak"]
    assert verbs.index("build-update-repo") < verbs.index("build-bundle")


def test_the_bundle_uses_the_url_with_a_trailing_slash(tmp_path):
    env = _env(tmp_path)
    env["NADARE_FLATPAK_URL"] = "https://flatpak.example"
    assert _build(tmp_path, env).returncode == 0
    bundle = next(c for c in _calls(env) if c[:2] == ["flatpak", "build-bundle"])
    assert f"--repo-url={URL}" in bundle


def test_a_failed_bundle_fails_the_build(tmp_path):
    env = _env(tmp_path)
    env["STUB_FAIL_BUNDLE"] = "1"
    result = _build(tmp_path, env)
    assert result.returncode != 0
    assert "ready for" not in result.stdout


def _built_repo(tmp_path: Path, *, bundle: bool) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("summary", "nadare.flatpakref", "index.flatpakrepo", "config"):
        (repo / name).write_text("x", encoding="utf-8")
    for sub in ("objects", "refs"):
        (repo / sub).mkdir()
    if bundle:
        (repo / "nadare.flatpak").write_text("BUNDLE", encoding="utf-8")
    return repo


def test_upload_publishes_the_bundle_fresh_with_its_media_type_and_last(tmp_path):
    env = _env(tmp_path)
    repo = _built_repo(tmp_path, bundle=True)
    result = subprocess.run(["bash", str(FLATPAK_DIR / "upload.sh"), str(repo)],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    calls = [c for c in _calls(env) if c[0] == "rclone"]
    bundle = [i for i, c in enumerate(calls) if str(repo / "nadare.flatpak") in c]
    assert len(bundle) == 1
    call = calls[bundle[0]]
    assert call[1] == "copyto" and call[-1].endswith(":nadare-flatpak/nadare.flatpak")
    assert "Cache-Control: no-cache, max-age=0" in call
    assert "Content-Type: application/vnd.flatpak" in call
    summary = next(i for i, c in enumerate(calls) if str(repo / "summary") in c)
    assert bundle[0] > summary


def test_upload_refuses_a_repository_without_the_bundle(tmp_path):
    env = _env(tmp_path)
    repo = _built_repo(tmp_path, bundle=False)
    result = subprocess.run(["bash", str(FLATPAK_DIR / "upload.sh"), str(repo)],
                            env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert not Path(env["STUB_LOG"]).exists()  # nothing was uploaded
