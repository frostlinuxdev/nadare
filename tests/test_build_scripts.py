# SPDX-License-Identifier: GPL-3.0-or-later
"""Regression guards for the build/CI tooling (scripts/, ci/, packaging/, docs).

These are mostly shell/config artefacts, so the guards are a mix of functional
checks (running a script) and static assertions on the exact patterns the audit
flagged; each one fails if the corresponding bug is reintroduced.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


def _maintainer_doc(rel: str) -> str:
    """A maintainer-only doc that the public snapshot does not ship (see
    ``scripts/make_public_snapshot.sh``): skip, rather than fail, without it."""
    path = _REPO_ROOT / rel
    if not path.is_file():
        pytest.skip(f"{rel} is not shipped in the public snapshot")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 07-3 — CI exists for the Nadare repo itself
# ---------------------------------------------------------------------------
class TestCIWorkflow:
    @pytest.mark.parametrize(
        "rel",
        [".forgejo/workflows/ci.yml", ".github/workflows/ci.yml"],
    )
    def test_workflow_runs_pytest_and_ruff_on_both_interpreters(self, rel):
        workflow = _REPO_ROOT / rel
        assert workflow.is_file(), "no CI workflow for the repository itself"
        text = workflow.read_text(encoding="utf-8")
        assert "python -m pytest tests -q" in text
        assert "ruff check src tests packaging scripts" in text
        assert '"3.12"' in text and '"3.13"' in text
        assert "QT_QPA_PLATFORM: offscreen" in text

    def test_forgejo_and_github_workflows_are_byte_identical(self):
        forgejo = (_REPO_ROOT / ".forgejo" / "workflows" / "ci.yml").read_bytes()
        github = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_bytes()
        assert forgejo == github, "update both workflow files together"


# ---------------------------------------------------------------------------
# 07-5 — the release appimagetool is pinned/verified
# ---------------------------------------------------------------------------
class TestAppimagetoolPin:
    def test_release_default_is_not_a_moving_tag(self):
        script = _text("scripts/build_appimage.sh")
        # The old moving-tag default must be gone, the verifier must exist, and
        # an explicit opt-out must be the only way to skip the digest.
        assert 'APPIMAGETOOL_RELEASE="${APPIMAGETOOL_RELEASE:-continuous}"' not in script
        assert "releases/download/continuous/appimagetool-x86_64.AppImage" not in script
        assert "APPIMAGETOOL_ALLOW_UNVERIFIED" in script
        assert "verify_appimagetool" in script

    def test_release_build_without_a_digest_fails_closed(self, tmp_path):
        """A release build (NADARE_RELEASE=1) must not ship an unverified tool."""
        tool = tmp_path / "appimagetool"
        tool.write_bytes(b"not the real appimagetool")
        env = dict(os.environ)
        env["APPIMAGETOOL"] = str(tool)
        env["NADARE_RELEASE"] = "1"
        for key in ("APPIMAGETOOL_SHA256", "APPIMAGETOOL_RELEASE", "APPIMAGETOOL_ALLOW_UNVERIFIED"):
            env.pop(key, None)
        result = subprocess.run(
            ["bash", str(_REPO_ROOT / "scripts" / "build_appimage.sh")],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        assert "APPIMAGETOOL_SHA256" in result.stderr

    def test_unset_digest_fails_closed_without_an_explicit_override(self, tmp_path):
        """Even a local build refuses an unverified tool unless it opts in."""
        tool = tmp_path / "appimagetool"
        tool.write_bytes(b"not the real appimagetool")
        env = dict(os.environ)
        env["APPIMAGETOOL"] = str(tool)
        for key in (
            "APPIMAGETOOL_SHA256",
            "APPIMAGETOOL_RELEASE",
            "APPIMAGETOOL_ALLOW_UNVERIFIED",
            "NADARE_RELEASE",
        ):
            env.pop(key, None)
        result = subprocess.run(
            ["bash", str(_REPO_ROOT / "scripts" / "build_appimage.sh")],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        assert "refusing to use an unverified appimagetool" in result.stderr

    def test_bad_digest_fails_closed_before_the_build(self, tmp_path):
        tool = tmp_path / "appimagetool"
        tool.write_bytes(b"not the real appimagetool")
        env = dict(os.environ)
        env["APPIMAGETOOL"] = str(tool)
        env["APPIMAGETOOL_SHA256"] = "0" * 64
        result = subprocess.run(
            ["bash", str(_REPO_ROOT / "scripts" / "build_appimage.sh")],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        assert "integrity check FAILED" in result.stderr


# ---------------------------------------------------------------------------
# release hardening: runtime pin (#80), version (#107), ELF class (#25), LICENSE (#7)
# ---------------------------------------------------------------------------
class TestAppImageReleaseHardening:
    @staticmethod
    def _env(tmp_path, **extra):
        import hashlib

        tool = tmp_path / "appimagetool"
        tool.write_bytes(b"stub appimagetool")
        env = dict(os.environ)
        for key in (
            "APPIMAGE_RUNTIME_RELEASE",
            "APPIMAGE_RUNTIME_SHA256",
            "APPIMAGE_RUNTIME_FILE",
            "NADARE_RELEASE",
            "NADARE_ALLOW_NO_KEYRING",
        ):
            env.pop(key, None)
        env.update(
            APPIMAGETOOL=str(tool),
            APPIMAGETOOL_SHA256=hashlib.sha256(tool.read_bytes()).hexdigest(),
            APPIMAGETOOL_RELEASE="1.9.1",
        )
        env.update(extra)
        return env

    @staticmethod
    def _run(env):
        return subprocess.run(
            ["bash", str(_REPO_ROOT / "scripts" / "build_appimage.sh")],
            capture_output=True,
            text=True,
            env=env,
            cwd=_REPO_ROOT,
        )

    def test_release_build_requires_a_pinned_runtime(self, tmp_path):
        env = self._env(tmp_path, NADARE_RELEASE="1", PYTHON="/bin/false")
        result = self._run(env)
        assert result.returncode != 0
        assert "APPIMAGE_RUNTIME_SHA256" in result.stderr

    def test_runtime_with_a_wrong_digest_fails_before_the_build(self, tmp_path):
        runtime = tmp_path / "runtime-x86_64"
        runtime.write_bytes(b"not the real runtime")
        env = self._env(
            tmp_path,
            NADARE_RELEASE="1",
            APPIMAGE_RUNTIME_FILE=str(runtime),
            APPIMAGE_RUNTIME_SHA256="0" * 64,
            PYTHON="/bin/false",
        )
        result = self._run(env)
        assert result.returncode != 0
        assert "runtime integrity check FAILED" in result.stderr

    def test_runtime_with_the_right_digest_gets_past_the_check(self, tmp_path):
        import hashlib

        runtime = tmp_path / "runtime-x86_64"
        runtime.write_bytes(b"pinned runtime bytes")
        env = self._env(
            tmp_path,
            NADARE_RELEASE="1",
            APPIMAGE_RUNTIME_FILE=str(runtime),
            APPIMAGE_RUNTIME_SHA256=hashlib.sha256(runtime.read_bytes()).hexdigest(),
            PYTHON="/bin/false",  # stop at the keyring check, before PyInstaller
        )
        result = self._run(env)
        assert "runtime verified" in result.stdout
        assert "integrity check FAILED" not in result.stderr

    def test_unreadable_version_fails_the_build(self, tmp_path):
        """No silent '0.1.0' fallback: it would mislabel the artifact (#107)."""
        py = tmp_path / "py"
        py.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *"import keyring"*) exit 0 ;;\n'
            '  *"import nadare"*) exit 1 ;;\n'
            "esac\nexit 0\n"
        )
        py.chmod(0o755)
        env = self._env(tmp_path, PYTHON=str(py))
        result = self._run(env)
        assert result.returncode != 0
        assert "cannot read nadare.__version__" in result.stderr

    def test_system_lib_lookup_is_64_bit_only_and_checked(self):
        script = _text("scripts/build_appimage.sh")
        assert "x86-64" in script  # ldconfig entries tagged for this arch
        assert "is_elf64" in script
        assert "cp -L \"$src\" \"$dest/\"" in script

    def test_license_is_staged_in_every_package(self):
        assert "LICENSE" in _text("scripts/build_appimage.sh").split("copy_system_libs libGL")[1]
        assert "LICENSE" in _text("scripts/build_flatpak.sh")
        assert "LICENSE" in _text("packaging/flatpak/io.github.frostlinuxdev.Nadare.yml")

    def test_other_packagers_do_not_fall_back_to_a_fake_version(self):
        assert "|| echo 0.1.0" not in _text("scripts/build_deb.sh")
        assert "|| echo 0.1.0" not in _text("scripts/build_appimage.sh")


# ---------------------------------------------------------------------------
# 07-6 / 07-7 — test_appimage.sh quoting, log path, and backend check
# ---------------------------------------------------------------------------
class TestAppimageSmokeScript:
    def test_path_is_passed_as_argv_not_spliced_into_the_shell(self):
        script = _text("scripts/test_appimage.sh")
        # The old injection vector: the path inside single quotes in a string.
        assert "cp '$appimage'" not in script
        assert "inx_argv" in script
        assert '_ "$appimage"' in script

    def test_warmup_log_is_a_private_mktemp_file(self):
        script = _text("scripts/test_appimage.sh")
        assert "/tmp/nadare-test-warmup.log" not in script
        assert "mktemp" in script
        assert 'trap \'rm -f "$warmup_log"\' EXIT' in script

    def test_backends_check_cannot_pass_vacuously(self):
        script = _text("scripts/test_appimage.sh")
        assert "grep -qv MISSING" not in script
        assert "grep -v MISSING | grep -q ." in script


# ---------------------------------------------------------------------------
# #310 — the AppImage bundles the Qt xcb platform stack
# ---------------------------------------------------------------------------
def _copy_system_libs_calls() -> list[str]:
    """Every top-level ``copy_system_libs`` invocation, continuations joined."""
    calls: list[str] = []
    current: list[str] | None = None
    for line in _text("scripts/build_appimage.sh").splitlines():
        if line.startswith("copy_system_libs "):
            current = [line]
        elif current is not None:
            current.append(line)
        if current is not None and not line.rstrip().endswith("\\"):
            calls.append(" ".join(current))
            current = None
    return calls


class TestBundledXcbPlatform:
    """PyInstaller already bundles the xcb stack; guard that it stays bundled."""

    REQUIRED = (
        "libxkbcommon-x11.so.0",
        "libxcb-cursor.so.0",
        "libxcb-icccm.so.4",
        "libxcb-util.so.1",
        "libxcb-image.so.0",
        "libxcb-keysyms.so.1",
        "libxcb-render-util.so.0",
        "libxcb-xkb.so.1",
        "libxcb-shape.so.0",
        "libxcb-sync.so.1",
        "libxcb-xfixes.so.0",
        "libxcb-randr.so.0",
        "libxcb-shm.so.0",
        "libxcb-render.so.0",
    )

    def test_copy_system_libs_does_not_sigpipe_ldconfig(self):
        """awk exiting at the first match SIGPIPEs ldconfig; with pipefail that is exit 141."""
        script = _text("scripts/build_appimage.sh")
        assert "set -euo pipefail" in script or "pipefail" in script
        assert "{ print $NF; exit }" not in script
        assert "!done" in script

    def test_smoke_script_runs_the_gui_on_xcb_under_xvfb(self):
        script = _text("scripts/test_appimage.sh")
        assert "command -v Xvfb" in script
        assert "xvfb-run -a" in script
        assert "QT_QPA_PLATFORM=xcb" in script
        assert "NADARE_SMOKE_EXIT_MS=2000" in script

    def test_smoke_script_checks_the_bundled_xcb_libraries_statically(self):
        script = _text("scripts/test_appimage.sh")
        for lib in self.REQUIRED:
            assert lib in script, f"{lib} must be checked inside the AppImage"

    def test_readme_no_longer_says_the_xcb_libraries_are_not_bundled(self):
        readme = _text("README.md")
        assert "does **not** bundle" not in readme
        assert "bundles the GL loader" in readme



# ---------------------------------------------------------------------------
# 07-9 — dev.sh setup uses the committed lock
# ---------------------------------------------------------------------------
class TestReleaseNaming:
    def test_appimage_name_comes_from_the_package_version(self):
        script = _text("scripts/build_appimage.sh")
        assert "Nadare-${version}-x86_64.AppImage" in script or "Nadare-$version" in script

    def test_appimage_build_writes_sha256sums(self):
        script = _text("scripts/build_appimage.sh")
        assert "SHA256SUMS" in script

    def test_contributing_documents_the_naming_rule(self):
        contributing = _text("CONTRIBUTING.md")
        assert "Nadare-<version>-x86_64.AppImage" in contributing
        assert "0.1.0-beta.1" in contributing  # named only as the form to avoid


class TestDevSetup:
    def test_setup_prefers_uv_sync_with_a_pip_fallback(self):
        script = _text("scripts/dev.sh")
        assert "uv sync --frozen --extra dev" in script
        # The pip path is retained only as a fallback.
        assert 'install -r requirements.txt' in script

    def test_setup_actually_calls_uv_sync(self, tmp_path, monkeypatch):
        import shutil

        repo = tmp_path / "repo"
        (repo / "scripts").mkdir(parents=True)
        shutil.copy(_REPO_ROOT / "scripts" / "dev.sh", repo / "scripts" / "dev.sh")
        venv_bin = repo / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        for name in ("python", "pip"):
            stub = venv_bin / name
            stub.write_text("#!/bin/sh\nexit 0\n")
            stub.chmod(0o755)
        (repo / "uv.lock").write_text("")

        bindir = tmp_path / "bin"
        bindir.mkdir()
        log = tmp_path / "uv.log"
        uv = bindir / "uv"
        uv.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n')
        uv.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")

        result = subprocess.run(
            ["bash", str(repo / "scripts" / "dev.sh"), "setup"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        calls = log.read_text()
        assert "sync" in calls and "--frozen" in calls
        assert "install -r requirements.txt" not in calls


# ---------------------------------------------------------------------------
# 07-8 / R4-5 — docs and packaging recipe match reality
# ---------------------------------------------------------------------------
class TestDocsMatchReality:
    def test_test_report_is_marked_a_snapshot(self):
        report = _maintainer_doc("docs/test-reports/2026-10-12.md")
        assert "Historical snapshot" in report
        # The dead link to an untracked scratch report is gone.
        assert "tests/live-network-report.md" not in report

    def test_appimage_builder_recipe_is_gone(self):
        assert not (_REPO_ROOT / "packaging" / "AppImageBuilder.yml").exists()

    def test_metainfo_version_matches_the_project_version(self):
        import tomllib

        project = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
        metainfo = _text("packaging/flatpak/io.github.frostlinuxdev.Nadare.metainfo.xml")
        assert f'<release version="{project}"' in metainfo

    def test_package_version_matches_the_project_version(self):
        import tomllib

        import nadare

        project = tomllib.loads(_text("pyproject.toml"))["project"]["version"]
        assert nadare.__version__ == project

    def test_testing_md_names_the_current_artifact_version(self):
        testing = _maintainer_doc("TESTING.md")
        # The stale pre-release artifact name (0.1.0) is gone, replaced by the
        # project version's spelling (0.1.0b1).
        assert "Nadare-0.1.0-x86_64.AppImage" not in testing
        assert "Nadare-0.1.0b1-x86_64.AppImage" in testing

    def test_contributing_does_not_call_the_recipe_current(self):
        contributing = _text("CONTRIBUTING.md")
        assert "AppImageBuilder" not in contributing


def test_certifi_is_pinned_past_the_emsign_incident():
    """curl_cffi trusts certifi's bundle; 2026.10.19 dropped the compromised eMudhra root."""
    pin = re.search(r"^certifi==([\d.]+)$", _text("requirements.txt"), re.M)
    assert pin is not None, "requirements.txt must pin certifi"
    assert tuple(int(p) for p in pin.group(1).split(".")) >= (2026, 10, 19)
    assert "certifi>=2026.10.19" in _text("pyproject.toml")


def test_the_appimage_spec_ships_the_endfield_helper_source():
    """endfield_shim compiles core/shim_src/suspend_start.c at run time, so the bundle needs it."""
    spec = _text("packaging/nadare.spec")
    assert re.search(r'"core",\s*"shim_src"\)[^)]*"nadare/core/shim_src"', spec), (
        "packaging/nadare.spec must ship nadare/core/shim_src (the AppImage lacked it in 0.1.0b7)"
    )
    assert (_REPO_ROOT / "src/nadare/core/shim_src/suspend_start.c").is_file()


def test_fetch_7zip_script_pins_source_and_digest():
    script = _text("scripts/fetch_7zip.sh")
    assert "22cf82fcc6eb7474f1d5bb4f02c0f8ebd573a718089d6833dc30f8350a3b232e" in script
    assert "https://github.com/ip7z/7zip/releases/download/26.05/7z2605-linux-x64.tar.xz" in script


def test_fetch_7zip_script_is_executable_and_called_through_bash():
    """The build once died with "Permission denied": the script had no execute bit."""
    import os
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "fetch_7zip.sh"
    assert os.access(script, os.X_OK), "scripts/fetch_7zip.sh must be executable"
    build = (root / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
    assert "bash scripts/fetch_7zip.sh" in build
