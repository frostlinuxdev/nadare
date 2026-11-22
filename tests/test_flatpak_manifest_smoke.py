# SPDX-License-Identifier: GPL-3.0-or-later
"""The Flatpak manifest is the release build's only description of the wrapper (#1596).

The release does not run ``flatpak-builder`` any more (#104): ``packaging/flatpak/stage_repo.py``
reads the app id, runtime, command, finish-args and modules out of
``packaging/flatpak/io.github.frostlinuxdev.Nadare.yml`` and stages the AppImage payload itself.
Nothing reads the manifest's source list, its desktop/metainfo/licence install commands or its
32-bit extensions, so a typo there only turned up in a release. These checks fail CI instead; the
``flatpak-manifest`` workflow runs them when packaging files change, and weekly.

Everything here is offline: the manifest names no download, only files in this repository and the
payload staged next to it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from xml.etree import ElementTree

import pytest

yaml = pytest.importorskip("yaml")

REPO = Path(__file__).resolve().parents[1]
FLATPAK_DIR = REPO / "packaging" / "flatpak"
MANIFEST = FLATPAK_DIR / "io.github.frostlinuxdev.Nadare.yml"
DESKTOP = REPO / "packaging" / "nadare.desktop"
METAINFO = MANIFEST.with_suffix(".metainfo.xml")

#: Built next to the manifest by the release recipe, never committed: the staged payload.
GENERATED_SOURCES = {"staging"}

#: The icon sizes the build installs. ``stage_repo.py`` renders exactly these (``for size in ...``).
ICON_SIZES = {128, 256, 512}

WORKFLOWS = (
    REPO / ".github" / "workflows" / "flatpak-manifest.yml",
    REPO / ".forgejo" / "workflows" / "flatpak-manifest.yml",
)


# --- the checks ------------------------------------------------------------------------------
def check_manifest(
    manifest_path: Path,
    *,
    repo_root: Path = REPO,
    flatpak_dir: Path = FLATPAK_DIR,
) -> list[str]:
    """Every internal inconsistency of the manifest, as plain sentences (empty = sound)."""
    path = Path(manifest_path)
    repo_root = Path(repo_root)
    flatpak_dir = Path(flatpak_dir)
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"manifest: not valid YAML: {exc}"]
    if not isinstance(manifest, dict):
        return ["manifest: must be a YAML mapping"]
    problems: list[str] = []

    for key in ("id", "runtime", "runtime-version", "sdk", "command", "modules"):
        if key not in manifest:
            problems.append(f"manifest: missing the {key!r} key")

    app_id = manifest.get("id")
    if not isinstance(app_id, str) or app_id.count(".") < 2:
        problems.append(f"id: {app_id!r} is not a reverse-DNS app id")
    elif app_id != path.stem:
        problems.append(f"id: {app_id!r} does not match the manifest file name {path.stem!r}")
    for name, pattern in (
        ("stage_repo.py", r'^APP_ID = "([^"]+)"'),
        ("build_repo.sh", r'^app_id="([^"]+)"'),
    ):
        source = flatpak_dir / name
        found = (
            re.search(pattern, source.read_text(encoding="utf-8"), re.M)
            if source.is_file()
            else None
        )
        if found is None:
            problems.append(f"id: {name} does not name the app id")
        elif found.group(1) != app_id:
            problems.append(f"id: {name} uses {found.group(1)!r}, not {app_id!r}")

    if not (flatpak_dir / f"{path.stem}.metainfo.xml").is_file():
        problems.append(f"metainfo: {path.stem}.metainfo.xml is missing next to the manifest")
    elif isinstance(app_id, str):
        root = ElementTree.parse(flatpak_dir / f"{path.stem}.metainfo.xml").getroot()
        if (root.findtext("id") or "").strip() != app_id:
            problems.append("metainfo: its <id> does not match the manifest id")
        launchable = root.find("launchable")
        if launchable is None or (launchable.text or "").strip() != f"{app_id}.desktop":
            problems.append("metainfo: its <launchable> does not name the app id's desktop file")

    command = manifest.get("command")
    commands = _build_commands(manifest)
    if not isinstance(command, str) or not command.strip():
        problems.append("command: must name the executable")
    elif not any(f"/app/bin/{command}" in line for line in commands):
        problems.append(f"command: no build command installs /app/bin/{command}")

    desktop = repo_root / "packaging" / "nadare.desktop"
    spellings = (f"{app_id}.desktop", "${FLATPAK_ID}.desktop") if isinstance(app_id, str) else ()
    if not desktop.is_file():
        problems.append("desktop entry: packaging/nadare.desktop is missing")
    else:
        executed = (_desktop_value(desktop, "Exec") or "").split()
        if isinstance(command, str) and (not executed or executed[0] != command):
            problems.append(f"desktop entry: Exec does not run the manifest command {command!r}")
        if not any(
            f"--set-icon={spelling.removesuffix('.desktop')}" in line
            for spelling in spellings
            for line in commands
        ):
            problems.append("desktop entry: no build command sets the icon to the app id")
        installed = [line for line in commands if "nadare.desktop" in line]
        if not any(
            f"share/applications/{spelling}" in line for spelling in spellings for line in installed
        ):
            problems.append("desktop entry: no build command installs it under the app id")
    if not (repo_root / "packaging" / "nadare.svg").is_file():
        problems.append("icon: packaging/nadare.svg is missing")

    for name, source in (
        ("nadare.desktop", desktop),
        ("LICENSE", repo_root / "LICENSE"),
        (f"{path.stem}.metainfo.xml", flatpak_dir / f"{path.stem}.metainfo.xml"),
    ):
        # The build commands may spell the app id out or leave it to ${FLATPAK_ID}.
        names = {name}
        if isinstance(app_id, str):
            names.add(name.replace(app_id, "${FLATPAK_ID}"))
        if not source.is_file():
            problems.append(f"build input: {name} does not exist in the repository")
        elif not any(spelling in line for spelling in names for line in commands):
            problems.append(f"build input: no build command installs {name}")

    icon_sizes: set[int] = set()
    for line in commands:
        match = re.search(r"for s in ([0-9 ]+);", line)
        if match and "nadare-$s.png" in line:
            icon_sizes = {int(size) for size in match.group(1).split()}
    if icon_sizes != ICON_SIZES:
        installed = sorted(icon_sizes) or "none"
        expected = sorted(ICON_SIZES)
        problems.append(f"icon sizes: the build installs {installed}, expected {expected}")

    for module in manifest.get("modules") or []:
        if not isinstance(module, dict):
            continue
        name = module.get("name") or "?"
        if not module.get("name"):
            problems.append("modules: a module has no name")
        module_commands = " ".join(module.get("build-commands") or [])
        for source in module.get("sources") or []:
            kind = source.get("type")
            if kind == "inline":
                dest = source.get("dest-filename")
                if not dest:
                    problems.append(f"module {name}: an inline source has no dest-filename")
                elif dest not in module_commands:
                    problems.append(
                        f"module {name}: inline source {dest!r} is not used by any build command"
                    )
            elif kind in ("dir", "file"):
                rel = source.get("path")
                if not rel:
                    problems.append(f"module {name}: a {kind} source has no path")
                elif rel not in GENERATED_SOURCES and not (flatpak_dir / rel).exists():
                    problems.append(f"module {name}: source path {rel!r} is not in the repository")
            elif kind is None:
                problems.append(f"module {name}: a source has no type")

    runtime, sdk = manifest.get("runtime"), manifest.get("sdk")
    version = str(manifest.get("runtime-version"))
    if not isinstance(runtime, str) or not isinstance(sdk, str):
        problems.append("runtime/sdk: must both be named")
    extensions = manifest.get("add-extensions") or {}
    for name, settings in extensions.items():
        for key in ("directory", "version"):
            if key not in settings:
                problems.append(f"add-extensions: {name} has no {key}")
    compat = extensions.get("org.freedesktop.Platform.Compat.i386") or {}
    if "version" in compat and str(compat["version"]) != version:
        problems.append(
            f"add-extensions: Compat.i386 version {compat['version']} "
            f"does not match runtime-version {version}"
        )
    gl32 = extensions.get("org.freedesktop.Platform.GL32") or {}
    branches = [branch.strip() for branch in str(gl32.get("versions") or "").split(";")]
    if gl32 and version not in branches:
        problems.append(f"add-extensions: GL32 versions does not list runtime-version {version}")
    if gl32 and str(gl32.get("version")) not in branches:
        problems.append(
            f"add-extensions: GL32 version {gl32.get('version')} is not in its versions"
        )

    finish_args = manifest.get("finish-args")
    if not isinstance(finish_args, list) or not finish_args:
        problems.append("finish-args: must be a non-empty list")
        finish_args = []
    for argument in finish_args:
        if not isinstance(argument, str) or not argument.startswith("--"):
            problems.append(f"finish-args: {argument!r} is not an argument")
    whole_home = {
        "--filesystem=home",
        "--filesystem=host",
        "--filesystem=~",
        "--filesystem=~/",
        "--filesystem=/home",
    }
    leaked = [argument for argument in finish_args if argument in whole_home]
    if leaked:
        problems.append(f"finish-args: grants the whole home folder ({', '.join(leaked)})")
    if "--env=NADARE_MANAGED_UPDATES=1" not in finish_args:
        problems.append("finish-args: must set NADARE_MANAGED_UPDATES=1 (Flatpak owns updates)")
    if "--env=NADARE_FLATPAK=1" not in finish_args:
        problems.append("finish-args: must set NADARE_FLATPAK=1 (sandbox detection)")
    return problems


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _build_commands(manifest: dict) -> list[str]:
    return [
        command
        for module in manifest.get("modules") or []
        if isinstance(module, dict)
        for command in module.get("build-commands") or []
    ]


def _desktop_value(path: Path, key: str) -> str | None:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


# --- the manifest is consistent --------------------------------------------------------------
def test_the_real_manifest_has_no_internal_inconsistency():
    assert check_manifest(MANIFEST) == []


# --- a broken copy in a temporary directory is caught ----------------------------------------
def _module(manifest: dict, name: str) -> dict:
    for module in manifest["modules"]:
        if module.get("name") == name:
            return module
    raise AssertionError(f"no module {name!r}")


def _inline_source(manifest: dict, dest_filename: str) -> dict:
    for module in manifest["modules"]:
        for source in module.get("sources") or []:
            if source.get("dest-filename") == dest_filename:
                return source
    raise AssertionError(f"no inline source {dest_filename!r}")


def _rename_app_id(manifest: dict) -> None:
    manifest["id"] = "io.github.example.Nadare"


def _rename_command(manifest: dict) -> None:
    manifest["command"] = "nadare-launcher"


def _drop_command(manifest: dict) -> None:
    del manifest["command"]


def _grant_the_whole_home(manifest: dict) -> None:
    manifest["finish-args"].append("--filesystem=home")


def _drop_managed_updates(manifest: dict) -> None:
    manifest["finish-args"].remove("--env=NADARE_MANAGED_UPDATES=1")


def _typo_the_source_path(manifest: dict) -> None:
    _module(manifest, "nadare")["sources"][0]["path"] = "staging-typo"


def _bump_the_compat_version(manifest: dict) -> None:
    manifest["add-extensions"]["org.freedesktop.Platform.Compat.i386"]["version"] = "24.08"


def _drop_the_gl32_runtime_version(manifest: dict) -> None:
    manifest["add-extensions"]["org.freedesktop.Platform.GL32"]["versions"] = "1.4"


def _drop_the_licence_install(manifest: dict) -> None:
    module = _module(manifest, "nadare")
    module["build-commands"] = [c for c in module["build-commands"] if "LICENSE" not in c]


def _rename_the_inline_file(manifest: dict) -> None:
    _inline_source(manifest, "ld.so.conf")["dest-filename"] = "ld.so.conf.d"


def _drop_an_icon_size(manifest: dict) -> None:
    module = _module(manifest, "nadare")
    module["build-commands"] = [
        command.replace("for s in 128 256 512;", "for s in 128 256;")
        for command in module["build-commands"]
    ]


def _install_desktop_under_another_name(manifest: dict) -> None:
    module = _module(manifest, "nadare")
    module["build-commands"] = [
        command.replace(
            "/app/share/applications/${FLATPAK_ID}.desktop",
            "/app/share/applications/nadare.desktop",
        )
        if command.startswith("install ")
        else command
        for command in module["build-commands"]
    ]


def _drop_the_desktop_icon(manifest: dict) -> None:
    module = _module(manifest, "nadare")
    module["build-commands"] = [
        command.replace("--set-icon=${FLATPAK_ID}", "--set-icon=nadare")
        for command in module["build-commands"]
    ]


BROKEN = [
    ("app id", _rename_app_id, "does not match the manifest file name"),
    ("command", _rename_command, "/app/bin/nadare-launcher"),
    ("missing command", _drop_command, "missing the 'command' key"),
    ("home access", _grant_the_whole_home, "whole home folder"),
    ("managed updates", _drop_managed_updates, "NADARE_MANAGED_UPDATES=1"),
    ("source path", _typo_the_source_path, "is not in the repository"),
    ("compat version", _bump_the_compat_version, "does not match runtime-version"),
    ("gl32 versions", _drop_the_gl32_runtime_version, "does not list runtime-version"),
    ("licence install", _drop_the_licence_install, "LICENSE"),
    ("inline file", _rename_the_inline_file, "is not used by any build command"),
    ("icon sizes", _drop_an_icon_size, "icon sizes"),
    ("desktop name", _install_desktop_under_another_name, "installs it under the app id"),
    ("desktop icon", _drop_the_desktop_icon, "sets the icon to the app id"),
]


@pytest.mark.parametrize(
    ("label", "break_it", "expected"), BROKEN, ids=[case[0] for case in BROKEN]
)
def test_a_broken_copy_of_the_manifest_is_reported(tmp_path, label, break_it, expected):
    data = _load(MANIFEST)
    break_it(data)
    broken = tmp_path / MANIFEST.name
    broken.write_text(yaml.safe_dump(data), encoding="utf-8")
    problems = check_manifest(broken, repo_root=REPO, flatpak_dir=FLATPAK_DIR)
    assert any(expected in problem for problem in problems), problems


def test_a_manifest_that_disagrees_with_its_metainfo_is_reported(tmp_path):
    """A copy of both files, so the metainfo is looked up next to the copy (as at build time)."""
    shadow = tmp_path / "flatpak"
    shadow.mkdir()
    (shadow / MANIFEST.name).write_text(MANIFEST.read_text(encoding="utf-8"), encoding="utf-8")
    metainfo = shadow / METAINFO.name
    metainfo.write_text(
        METAINFO.read_text(encoding="utf-8").replace(
            f"<id>{MANIFEST.stem}</id>", "<id>io.github.example.Nadare</id>"
        ),
        encoding="utf-8",
    )
    problems = check_manifest(shadow / MANIFEST.name, repo_root=REPO, flatpak_dir=shadow)
    assert any("metainfo: its <id>" in problem for problem in problems), problems


# --- the CI job -------------------------------------------------------------------------------
def _on(document: dict) -> dict:
    # Unquoted ``on`` is a YAML 1.1 boolean, which PyYAML reads as True.
    return document.get("on") or document[True]


def test_the_smoke_job_is_the_same_in_both_workflow_trees():
    for workflow in WORKFLOWS:
        assert workflow.is_file(), f"no Flatpak manifest smoke job at {workflow}"
    github, forgejo = (workflow.read_bytes() for workflow in WORKFLOWS)
    assert github == forgejo, "update both Flatpak workflow files together"


def test_the_smoke_job_runs_only_on_packaging_changes_or_a_schedule():
    on = _on(_load(WORKFLOWS[0]))
    pull_request = on.get("pull_request")
    assert isinstance(pull_request, dict), "a bare pull_request would run it on every PR"
    paths = pull_request.get("paths") or []
    assert "packaging/flatpak/**" in paths
    assert "tests/test_flatpak_manifest_smoke.py" in paths
    assert on.get("schedule"), "the job must also run on a schedule"
    assert "workflow_dispatch" in on, "the job must be runnable by hand"


def test_the_smoke_job_runs_the_manifest_test_with_the_locked_versions():
    text = WORKFLOWS[0].read_text(encoding="utf-8")
    assert "tests/test_flatpak_manifest_smoke.py" in text
    lock = tomllib.loads((REPO / "uv.lock").read_text(encoding="utf-8"))
    versions = {package["name"].lower(): package["version"] for package in lock["package"]}
    for name in ("pytest", "pyyaml"):
        match = re.search(rf"\b{name}==([\w.]+)", text, re.I)
        assert match is not None, f"the job must pin {name} to the version in uv.lock"
        assert match.group(1) == versions[name], (name, match.group(1), versions[name])


def test_the_smoke_job_pins_its_actions_to_a_commit():
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOWS[0].read_text(encoding="utf-8"))
    assert uses
    unpinned = [action for action in uses if not re.search(r"@[0-9a-f]{40}$", action)]
    assert unpinned == [], unpinned
