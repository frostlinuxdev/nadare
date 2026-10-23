# SPDX-License-Identifier: GPL-3.0-or-later
"""Ubuntu's AppArmor user-namespace restriction (#429) and direct Proton (#430)."""

from __future__ import annotations

from pathlib import Path

from nadare.app import run_cli
from nadare.core import graphics, process, runners, sandbox, userns

BWRAP_FAILURE = (
    "pressure-vessel-wrap[1778]: E: Child process exited with code 1: "
    "bwrap: setting up uid map: Permission denied"
)


def test_restricted_reads_the_sysctl(tmp_path: Path) -> None:
    on = tmp_path / "on"
    on.write_text("1\n")
    off = tmp_path / "off"
    off.write_text("0\n")
    assert userns.restricted(str(on)) is True
    assert userns.restricted(str(off)) is False


def test_restricted_is_false_when_the_sysctl_does_not_exist(tmp_path: Path) -> None:
    # Fedora, Arch, Debian...: no such knob, so nothing is restricted.
    assert userns.restricted(str(tmp_path / "missing")) is False


def test_restricted_ignores_garbage(tmp_path: Path) -> None:
    junk = tmp_path / "junk"
    junk.write_text("maybe\n")
    assert userns.restricted(str(junk)) is False


def test_profile_installed_checks_the_file(tmp_path: Path) -> None:
    profile = tmp_path / "nadare-srt-bwrap"
    assert userns.profile_installed(str(profile)) is False
    profile.write_text("x")
    assert userns.profile_installed(str(profile)) is True


def test_failure_line_matches_the_known_bwrap_errors() -> None:
    assert userns.is_failure_line(BWRAP_FAILURE)
    assert userns.is_failure_line("bwrap: No permissions to create new namespace")
    assert userns.is_failure_line(
        "bwrap: Creating new namespace failed: Operation not permitted"
    )


def test_failure_line_ignores_other_output() -> None:
    assert not userns.is_failure_line("")
    assert not userns.is_failure_line("INFO: steamrt4 is up to date")
    assert not userns.is_failure_line("bwrap: execvp wine: No such file or directory")


def test_profile_text_is_scoped_to_the_bundled_bwrap() -> None:
    text = userns.profile_text()
    assert "profile nadare-srt-bwrap" in text
    assert (
        "@{HOME}/.local/share/umu/*/pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap"
        in text
    )
    assert "flags=(unconfined)" in text
    assert "userns," in text
    # It must never become a blanket allow.
    assert "profile nadare-srt-bwrap /" not in text
    assert "profile nadare-srt-bwrap {" not in text


def test_install_command_writes_and_loads_the_profile() -> None:
    cmd = userns.install_command()
    assert "sudo tee /etc/apparmor.d/nadare-srt-bwrap" in cmd
    assert "sudo apparmor_parser -r /etc/apparmor.d/nadare-srt-bwrap" in cmd
    assert userns.profile_text().strip() in cmd


def test_hint_names_apparmor_and_the_fix() -> None:
    text = userns.hint()
    assert "AppArmor" in text
    assert "kernel.apparmor_restrict_unprivileged_userns" in text
    assert userns.install_command() in text
    # The old text blamed the GPU; this one must not.
    assert "GPU" not in text


def test_with_hint_appends_only_when_blocked() -> None:
    assert userns.with_hint("install failed", blocked=False) == "install failed"
    out = userns.with_hint("install failed", blocked=True)
    assert out.startswith("install failed")
    assert userns.hint() in out


def test_doctor_reports_a_restricted_host(monkeypatch, capsys) -> None:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(userns, "restricted", lambda *a, **k: True)
    monkeypatch.setattr(userns, "profile_installed", lambda *a, **k: False)

    code = run_cli(["doctor"])
    out = capsys.readouterr().out
    assert code == 0  # informational: the sysctl alone does not prove a failure
    assert "user namespaces: restricted by AppArmor" in out
    assert "sudo apparmor_parser -r /etc/apparmor.d/nadare-srt-bwrap" in out


def test_doctor_is_quiet_when_the_profile_is_installed(monkeypatch, capsys) -> None:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(userns, "restricted", lambda *a, **k: True)
    monkeypatch.setattr(userns, "profile_installed", lambda *a, **k: True)

    run_cli(["doctor"])
    out = capsys.readouterr().out
    assert "user namespaces: restricted, Nadare's AppArmor profile is installed" in out
    assert "sudo tee" not in out


def test_doctor_says_ok_on_an_unrestricted_host(monkeypatch, capsys) -> None:
    monkeypatch.setattr(graphics, "vulkan_info", lambda **k: graphics.VulkanInfo(True))
    monkeypatch.setattr(graphics, "dxvk_ok", lambda: True)
    monkeypatch.setattr(process, "which", lambda name, **k: "/usr/bin/umu-run")
    monkeypatch.setattr(runners, "all_runners", list)
    monkeypatch.setattr(userns, "restricted", lambda *a, **k: False)

    run_cli(["doctor"])
    assert "user namespaces: OK" in capsys.readouterr().out


# ---- #430: direct Proton needs STEAM_COMPAT_CLIENT_INSTALL_PATH ------------------
def test_proton_direct_sets_a_private_client_install_dir(tmp_path: Path) -> None:
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    _argv, env = sandbox.proton_direct(tmp_path / "proton-dir", prefix, ["wineboot", "-u"])
    client = Path(env["STEAM_COMPAT_CLIENT_INSTALL_PATH"])
    assert client.is_dir()
    # #36: never the game prefix, and never somewhere that looks like a real Steam.
    assert client != prefix
    assert prefix not in client.parents
    assert "Steam" not in client.parts
    from nadare.core import paths

    assert paths.data_dir() in client.parents
    assert env["STEAM_COMPAT_DATA_PATH"] == str(prefix)


# ---- CZN: the failure is explained instead of blamed on the GPU ------------------
def _czn(tmp_path: Path):
    from _backend_helpers import make_context

    from nadare.backends.czn.backend import CznBackend

    ctx = make_context(tmp_path, "stove-czn", {"prefix": str(tmp_path / "prefix")}, "test")
    return CznBackend(ctx)


def test_czn_flags_a_bwrap_uid_map_failure_in_the_output(tmp_path: Path) -> None:
    backend = _czn(tmp_path)
    assert backend._userns_blocked is False
    backend._emit_text("INFO: steamrt4 is up to date")
    assert backend._userns_blocked is False
    backend._emit_text(BWRAP_FAILURE)
    assert backend._userns_blocked is True


def test_czn_prefix_failure_names_apparmor_when_bwrap_was_blocked(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    from nadare.core.backends import BackendError

    backend = _czn(tmp_path)

    def fake_run(args):
        backend._emit_text(BWRAP_FAILURE)
        return 1

    monkeypatch.setattr(backend, "_run_in_prefix", fake_run)
    with pytest.raises(BackendError) as err:
        backend._ensure_prefix()
    assert "prefix creation failed (exit 1)" in str(err.value)
    assert "AppArmor" in str(err.value)
    assert userns.install_command() in str(err.value)


def test_czn_prefix_failure_is_unchanged_for_other_errors(tmp_path: Path, monkeypatch) -> None:
    import pytest

    from nadare.core.backends import BackendError

    backend = _czn(tmp_path)
    monkeypatch.setattr(backend, "_run_in_prefix", lambda args: 1)
    with pytest.raises(BackendError) as err:
        backend._ensure_prefix()
    assert str(err.value) == "prefix creation failed (exit 1)"
