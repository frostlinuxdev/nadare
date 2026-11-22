# SPDX-License-Identifier: GPL-3.0-or-later
"""The self-hosted Flatpak repo is staged from the trimmed AppImage payload, per the manifest."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "packaging" / "flatpak" / "stage_repo.py"
MANIFEST = REPO / "packaging" / "flatpak" / "io.github.frostlinuxdev.Nadare.yml"


@pytest.fixture(scope="module")
def stage():
    spec = importlib.util.spec_from_file_location("stage_repo_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finish_arguments_match_the_manifest(stage):
    manifest = yaml.safe_load(MANIFEST.read_text())
    argv = stage.finish_argv(manifest, "root")
    assert argv[:3] == ["flatpak", "build-finish", "--command=nadare"]
    assert argv[-1] == "root"
    for arg in manifest["finish-args"]:
        assert arg in argv
    assert "--talk-name=org.freedesktop.Flatpak" in argv  # Nadare installs missing extensions
    assert any(a.startswith("--extension=org.freedesktop.Platform.GL32=") for a in argv)
    assert any(a.startswith("--extension=org.freedesktop.Platform.Compat.i386=") for a in argv)


def test_the_runtime_provided_libraries_are_removed(stage):
    names = stage.removed_libraries(yaml.safe_load(MANIFEST.read_text()))
    assert "libvulkan.so.1" in names and "libfontconfig.so.1" in names
    assert all("/" not in name for name in names)


@pytest.fixture()
def no_icon_tools(stage, monkeypatch):
    """The icon is not what these tests check: CI runners lack rsvg-convert and ImageMagick."""

    def fake_render(svg, size, target):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_bytes(b"\x89PNG fake")

    monkeypatch.setattr(stage, "render_icon", fake_render)


def test_staging_keeps_symlinks_and_never_mirrors_flathub(stage, tmp_path, no_icon_tools):
    appdir = tmp_path / "AppDir" / "usr" / "bin"
    internal = appdir / "_internal"
    (internal / "PyQt6" / "Qt6" / "lib").mkdir(parents=True)
    (internal / "PyQt6" / "Qt6" / "lib" / "libQt6Core.so.6").write_bytes(b"qt")
    (internal / "libQt6Core.so.6").symlink_to("PyQt6/Qt6/lib/libQt6Core.so.6")
    (internal / "libvulkan.so.1").write_bytes(b"old vulkan")
    (appdir / "nadare").write_bytes(b"\x7fELF")
    out = tmp_path / "root"
    _bind(stage, tmp_path / "AppDir")
    stage.stage(tmp_path / "AppDir", out, proton_libs=False, **EXPECTED)
    staged = out / "files" / "lib" / "nadare" / "_internal"
    assert (staged / "libQt6Core.so.6").is_symlink()  # Qt stored once
    assert not (staged / "libvulkan.so.1").exists()  # the runtime's copy is used
    assert (
        out / "files" / "share" / "metainfo" / "io.github.frostlinuxdev.Nadare.metainfo.xml"
    ).is_file()
    assert not (out / "files" / "lib" / "i386-linux-gnu" / "libc.so.6").exists()


def test_the_desktop_entry_has_each_key_once(stage, tmp_path, no_icon_tools):
    # desktop-file-validate (run by flatpak build-finish/export) rejects a repeated key.
    appdir = tmp_path / "AppDir" / "usr" / "bin"
    (appdir / "_internal").mkdir(parents=True)
    (appdir / "nadare").write_bytes(b"\x7fELF")
    out = tmp_path / "root"
    _bind(stage, tmp_path / "AppDir")
    stage.stage(tmp_path / "AppDir", out, proton_libs=False, **EXPECTED)
    entry = (
        out / "files" / "share" / "applications" / "io.github.frostlinuxdev.Nadare.desktop"
    ).read_text()
    keys = [line.split("=", 1)[0] for line in entry.splitlines() if "=" in line]
    assert len(keys) == len(set(keys)), sorted(k for k in keys if keys.count(k) > 1)
    assert "StartupWMClass=nadare" in entry.splitlines()
    assert "Icon=io.github.frostlinuxdev.Nadare" in entry.splitlines()


def test_a_used_staging_directory_is_refused(stage, tmp_path):
    (tmp_path / "out").mkdir()
    with pytest.raises(SystemExit):
        stage.stage(tmp_path / "AppDir", tmp_path / "out")


def test_the_app_gets_x11_on_a_wayland_desktop_too():
    """`fallback-x11` means no X11 when Wayland is there (measured: no DISPLAY, no socket), so
    a game set to Display backend X11 silently ran on Wayland. Adding `x11` next to it is not
    enough: the fallback rule still wins. Games need XWayland as the escape hatch."""
    args = yaml.safe_load(MANIFEST.read_text())["finish-args"]
    assert "--socket=x11" in args
    assert "--socket=fallback-x11" not in args
    assert "--socket=wayland" in args


def test_the_icon_is_never_rendered_with_imagemagick(stage, tmp_path, monkeypatch):
    """ImageMagick's own SVG reader drops the gradient and the rotated arms: the b24 beta
    Flatpak showed a black square with a dot in the menu. Only rsvg-convert renders it."""

    calls = []
    monkeypatch.setattr(
        stage.shutil,
        "which",
        lambda name, *a, **k: f"/usr/bin/{name}" if name == "magick" else None,
    )
    monkeypatch.setattr(stage.subprocess, "run", lambda argv, **k: calls.append(argv))
    with pytest.raises(SystemExit, match="rsvg-convert"):
        stage.render_icon(tmp_path / "x.svg", 128, tmp_path / "out" / "x.png")
    assert calls == []


def test_the_icon_is_rendered_with_rsvg_convert(stage, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(stage.shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    monkeypatch.setattr(stage.subprocess, "run", lambda argv, **k: calls.append(argv))
    stage.render_icon(tmp_path / "x.svg", 128, tmp_path / "out" / "x.png")
    assert [argv[0] for argv in calls] == ["rsvg-convert"]


def test_flatpak_stage_rejects_payload_without_source_identity(stage, tmp_path, no_icon_tools):
    payload = tmp_path / "AppDir" / "usr" / "bin"
    payload.mkdir(parents=True)
    (payload / "nadare").write_bytes(b"old executable")
    with pytest.raises(SystemExit, match="identity"):
        stage.stage(tmp_path / "AppDir", tmp_path / "out", proton_libs=False)
    assert not (tmp_path / "out").exists()


EXPECTED = dict(expected_version="1.2.3", expected_commit="a" * 40, expected_lock_sha256="b" * 64)


def _bind(stage, appdir):
    import json

    identity = {
        "schema": 1,
        "verified": True,
        "version": EXPECTED["expected_version"],
        "source_commit": EXPECTED["expected_commit"],
        "uv_lock_sha256": EXPECTED["expected_lock_sha256"],
        "payload_sha256": stage.identity.payload_digest(appdir / "usr" / "bin"),
        "environment": {"nadare-launcher": "1.2.3", "pyinstaller": "6.20"},
        "transformations": ["strip --strip-debug shared libraries"],
    }
    (appdir / "build-identity.json").write_text(json.dumps(identity))
    return identity


@pytest.fixture
def bound_payload(stage, tmp_path):
    appdir = tmp_path / "AppDir"
    payload = appdir / "usr" / "bin"
    (payload / "_internal").mkdir(parents=True)
    (payload / "nadare").write_bytes(b"synthetic executable")
    (payload / "nadare").chmod(0o755)
    (payload / "_internal" / "libvulkan.so.1").write_bytes(b"stripped later")
    _bind(stage, appdir)
    return appdir


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "0.0.1"),
        ("source_commit", "c" * 40),
        ("uv_lock_sha256", "d" * 64),
        ("verified", False),
        ("schema", 2),
    ],
)
def test_flatpak_rejects_mismatched_identity(stage, tmp_path, bound_payload, field, value):
    import json

    marker = bound_payload / "build-identity.json"
    data = json.loads(marker.read_text())
    data[field] = value
    marker.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match="identity"):
        stage.stage(bound_payload, tmp_path / "out", proton_libs=False, **EXPECTED)
    assert not (tmp_path / "out").exists()


def test_flatpak_rejects_missing_marker_with_explicit_expectations(stage, tmp_path, bound_payload):
    (bound_payload / "build-identity.json").unlink()
    with pytest.raises(SystemExit, match="identity"):
        stage.stage(bound_payload, tmp_path / "out", proton_libs=False, **EXPECTED)


@pytest.mark.parametrize("tamper", ["bytes", "mode", "symlink"])
def test_flatpak_rejects_tampered_payload(stage, tmp_path, bound_payload, tamper):
    target = bound_payload / "usr" / "bin" / "nadare"
    if tamper == "bytes":
        target.write_bytes(b"tampered")
    elif tamper == "mode":
        target.chmod(0o644)
    else:
        (target.parent / "link").symlink_to("nadare")
    with pytest.raises(SystemExit, match="digest"):
        stage.stage(bound_payload, tmp_path / "out", proton_libs=False, **EXPECTED)
    assert not (tmp_path / "out").exists()


def test_flatpak_admits_bound_fixture_and_records_transformed_digest(
    stage,
    tmp_path,
    bound_payload,
    no_icon_tools,
):
    import json

    out = tmp_path / "out"
    stage.stage(bound_payload, out, proton_libs=False, **EXPECTED)
    record = json.loads((out / "files" / "share" / "nadare" / "build-identity.json").read_text())
    assert record["verified"] is True
    assert record["input_identity"] == json.loads(
        (bound_payload / "build-identity.json").read_text()
    )
    assert record["payload_sha256"] == stage.identity.payload_digest(
        out / "files",
        exclude={
            "share/nadare/build-identity.json",
        },
    )
    assert "libvulkan.so.1" in record["transformations"]["removed_libraries"]
    assert record["transformations"]["proton_libraries"] == []


def test_stale_appdir_is_rejected_even_when_a_fresh_bundle_exists(stage, tmp_path, bound_payload):
    fresh = tmp_path / "dist" / "nadare"
    fresh.mkdir(parents=True)
    (fresh / "nadare").write_bytes(b"fresh build")
    with pytest.raises(SystemExit, match="identity"):
        stage.stage(
            bound_payload,
            tmp_path / "out",
            proton_libs=False,
            **{**EXPECTED, "expected_commit": "f" * 40},
        )
    assert not (tmp_path / "out").exists()


def test_development_payload_requires_opt_in_and_stays_unverified(stage, tmp_path, no_icon_tools):
    import json

    appdir = tmp_path / "AppDir"
    (appdir / "usr" / "bin").mkdir(parents=True)
    (appdir / "usr" / "bin" / "nadare").write_bytes(b"development")
    stage.stage(appdir, tmp_path / "out", proton_libs=False, allow_unverified=True)
    data = json.loads((tmp_path / "out/files/share/nadare/build-identity.json").read_text())
    assert data["verified"] is False
    assert data["verification_errors"]


def test_verified_staging_requires_all_expected_inputs(stage, tmp_path, bound_payload):
    with pytest.raises(SystemExit, match="expected"):
        stage.stage(bound_payload, tmp_path / "out", proton_libs=False)


@pytest.mark.parametrize("field,value", [("source_commit", 17), ("uv_lock_sha256", "invalid")])
def test_malformed_identity_fails_cleanly(stage, tmp_path, bound_payload, field, value):
    import json

    path = bound_payload / "build-identity.json"
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    expected = dict(EXPECTED)
    if field == "uv_lock_sha256":
        expected["expected_lock_sha256"] = value
    with pytest.raises(SystemExit, match="identity"):
        stage.stage(bound_payload, tmp_path / "out", proton_libs=False, **expected)


def test_payload_changed_during_copy_is_rejected(stage, tmp_path, bound_payload, monkeypatch):
    original = stage.shutil.copytree

    def tampered_copy(*args, **kwargs):
        result = original(*args, **kwargs)
        target = Path(args[1]) / "nadare"
        if target.is_file():
            target.write_bytes(b"changed during copy")
        return result

    monkeypatch.setattr(stage.shutil, "copytree", tampered_copy)
    with pytest.raises(SystemExit, match="digest changed"):
        stage.stage(bound_payload, tmp_path / "out", proton_libs=False, **EXPECTED)
    assert not (tmp_path / "out").exists()


def test_development_override_never_labels_output_verified(
    stage, tmp_path, bound_payload, no_icon_tools,
):
    import json

    out = tmp_path / "out"
    stage.stage(bound_payload, out, proton_libs=False, allow_unverified=True, **EXPECTED)
    data = json.loads((out / "files/share/nadare/build-identity.json").read_text())
    assert data["verified"] is False
    assert data["verification_errors"]
