# SPDX-License-Identifier: GPL-3.0-or-later
"""Secure mode: the bubblewrap command that hides the real home from a game (#804)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nadare.core import gamesandbox


@pytest.fixture(autouse=True)
def _own_state_folder(tmp_path, monkeypatch):
    """The trusted-runtime record lives in the state folder: never the real one."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def layout(tmp_path):
    home = tmp_path / "home" / "user"
    prefix = home / ".local/share/nadare/profiles/g/prefix"
    runner = home / ".local/share/nadare/runners/GE-Proton"
    runtime = tmp_path / "run" / "user" / "1000"
    for d in (prefix, runner, runtime / "pulse", home / ".local/share/umu"):
        d.mkdir(parents=True)
    for name in (
        "wayland-0",
        "wayland-0.lock",
        "gamescope-0",
        "gamescope-0-lock",
        "pipewire-0",
        "bus",
        "keyring",
        "gnupg",
    ):
        (runtime / name).touch()
    env = {
        "HOME": str(home),
        "XDG_RUNTIME_DIR": str(runtime),
        # As process.build_env sets them for a real launch: the XDG roots, not Nadare's folders.
        "XDG_DATA_HOME": str(home / ".local/share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_STATE_HOME": str(home / ".local/state"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "DISPLAY": ":0",
        "WAYLAND_DISPLAY": "wayland-0",
        "WINEPREFIX": str(prefix),
    }
    for key in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        (Path(env[key]) / "nadare").mkdir(parents=True)
    return {"home": home, "prefix": prefix, "runner": runner, "runtime": runtime, "env": env}


def _wrap(layout, argv=("umu-run", "game.exe"), **kw):
    return gamesandbox.wrap(
        list(argv),
        layout["env"],
        prefix=layout["prefix"],
        home=layout["home"],
        runner_dir=kw.pop("runner_dir", layout["runner"]),
        **kw,
    )


def _pairs(flags, name):
    """Every (src, dest) following ``name`` in a flat bwrap argument list."""
    return [(flags[i + 1], flags[i + 2]) for i, f in enumerate(flags) if f == name]


def _flags(argv):
    return argv[1 : argv.index("--")]


def test_available_follows_which(monkeypatch):
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: None)
    assert gamesandbox.available() is False
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    assert gamesandbox.available() is True


def test_the_original_command_follows_the_double_dash(layout):
    argv, _env = _wrap(layout)
    assert argv[0] == "bwrap"
    assert argv[argv.index("--") + 1 :] == ["umu-run", "game.exe"]


def test_the_original_argv_and_env_are_not_mutated(layout):
    original = ["umu-run", "game.exe"]
    before = dict(layout["env"])
    gamesandbox.wrap(
        original, layout["env"], prefix=layout["prefix"], home=layout["home"], runner_dir=None
    )
    assert original == ["umu-run", "game.exe"]
    assert layout["env"] == before


def test_the_home_is_replaced_by_an_empty_tmpfs_before_anything_is_bound_back(layout):
    flags = _flags(_wrap(layout)[0])
    tmpfs_at = flags.index("--tmpfs")
    assert flags[tmpfs_at + 1] == str(layout["home"])
    first_bind_back = min(
        i for i, f in enumerate(flags) if f in ("--bind", "--ro-bind") and flags[i + 1] != "/"
    )
    assert tmpfs_at < first_bind_back


def test_a_symlinked_home_is_resolved_first(layout, tmp_path):
    """Fedora Atomic: /home -> var/home. Hiding the symlink fails; hide what it points to."""
    link = tmp_path / "homelink"
    link.symlink_to(layout["home"], target_is_directory=True)
    argv, _ = gamesandbox.wrap(
        ["x"], layout["env"], prefix=layout["prefix"], home=link, runner_dir=None
    )
    flags = _flags(argv)
    assert flags[flags.index("--tmpfs") + 1] == str(layout["home"].resolve())


def test_the_prefix_is_bound_read_write_and_resolved(layout, tmp_path):
    real = tmp_path / "elsewhere" / "prefix"
    real.mkdir(parents=True)
    link = layout["home"] / "prefix-link"
    link.symlink_to(real, target_is_directory=True)
    argv, _ = gamesandbox.wrap(["x"], layout["env"], prefix=link, home=layout["home"])
    assert (str(real), str(real)) in _pairs(_flags(argv), "--bind")


def test_an_install_dir_outside_the_prefix_is_bound_and_one_inside_is_not_repeated(
    layout, tmp_path
):
    outside = tmp_path / "games" / "NIKKE"
    outside.mkdir(parents=True)
    argv, _ = _wrap(layout, install_dir=outside)
    assert (str(outside), str(outside)) in _pairs(_flags(argv), "--bind")

    inside = layout["prefix"] / "drive_c" / "Game"
    inside.mkdir(parents=True)
    binds = _pairs(_flags(_wrap(layout, install_dir=inside)[0]), "--bind")
    assert (str(inside), str(inside)) not in binds


def test_the_runner_is_read_only(layout):
    flags = _flags(_wrap(layout)[0])
    assert (str(layout["runner"]), str(layout["runner"])) in _pairs(flags, "--ro-bind")
    assert (str(layout["runner"]), str(layout["runner"])) not in _pairs(flags, "--bind")


def test_only_the_umu_runtime_and_a_cache_folder_come_back_of_the_xdg_folders(layout):
    """Nadare's config, data and state stay hidden: the prefix and install dir are bound alone."""
    binds = {src for src, _ in _pairs(_flags(_wrap(layout)[0]), "--bind")}
    home = layout["home"]
    for hidden in (".config/nadare", ".local/share/nadare", ".local/state/nadare"):
        assert str(home / hidden) not in binds
    assert str(home / ".local/share/umu") in binds
    assert str(layout["prefix"]) in binds


def test_the_shader_cache_lives_in_a_bound_folder_of_its_own(layout):
    argv, env = _wrap(layout)
    cache = Path(env["XDG_CACHE_HOME"])
    assert cache == layout["home"] / ".cache/nadare/sandbox-cache"
    assert (str(cache), str(cache)) in _pairs(_flags(argv), "--bind")
    assert str(layout["home"] / ".cache") not in {s for s, _ in _pairs(_flags(argv), "--bind")}


def test_the_runtime_bubblewrap_cannot_be_replaced_by_the_game(layout, monkeypatch):
    """Nadare runs this file outside the Sandbox on the next launch."""
    srt = layout["home"] / ".local/share/umu/steamrt3/pressure-vessel/libexec/srt-bwrap"
    srt.parent.mkdir(parents=True)
    srt.write_text("#!/bin/sh\n")
    monkeypatch.setattr(gamesandbox, "runtime_copies", lambda: [str(srt)])
    flags = _flags(_wrap(layout)[0])
    assert (str(srt), str(srt)) in _pairs(flags, "--ro-bind")
    order = [(f, flags[i + 1]) for i, f in enumerate(flags) if f in ("--bind", "--ro-bind")]
    umu = str(layout["home"] / ".local/share/umu")
    assert order.index(("--ro-bind", str(srt))) > order.index(("--bind", umu))


def test_the_whole_xdg_folders_stay_hidden(layout):
    """Binding ~/.config or ~/.local/share would show a game every other app's data."""
    flags = _flags(_wrap(layout)[0])
    bound = {src for flag in ("--bind", "--ro-bind") for src, _ in _pairs(flags, flag)}
    for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        assert layout["env"][key] not in bound


def test_other_drives_are_hidden(layout):
    flags = _flags(_wrap(layout)[0])
    hidden = [flags[i + 1] for i, f in enumerate(flags) if f == "--tmpfs"]
    present = [p for p in ("/mnt", "/media", "/run/media") if Path(p).is_dir()]
    assert present, "this machine has none of the mount points: the test proves nothing"
    for path in present:
        assert path in hidden


def test_a_symlinked_mount_point_is_hidden_by_its_target(layout, tmp_path, monkeypatch):
    """Bazzite and Silverblue have /mnt -> var/mnt: bwrap cannot mount a tmpfs on a symlink."""
    real = tmp_path / "var-mnt"
    real.mkdir()
    link = tmp_path / "mnt"
    link.symlink_to(real)
    monkeypatch.setattr(gamesandbox, "_HIDDEN_MOUNTS", (str(link), str(link)))
    flags = _flags(_wrap(layout)[0])
    hidden = [flags[i + 1] for i, f in enumerate(flags) if f == "--tmpfs"]
    assert str(link) not in hidden
    assert hidden.count(str(real)) == 1


def test_the_session_runtime_dir_is_masked_and_only_display_and_sound_come_back(layout):
    flags = _flags(_wrap(layout)[0])
    runtime = str(layout["runtime"])
    assert runtime in [flags[i + 1] for i, f in enumerate(flags) if f == "--tmpfs"]
    back = {Path(src).name for src, _ in _pairs(flags, "--bind")} | {
        Path(src).name for src, _ in _pairs(flags, "--ro-bind")
    }
    assert {"wayland-0", "pipewire-0", "pulse"} <= back
    # the session bus, the keyring and the gpg agent stay hidden
    assert not back & {"bus", "keyring", "gnupg", "wayland-0.lock"}


def test_the_display_variables_survive_and_the_env_is_a_copy(layout):
    _argv, env = _wrap(layout)
    assert env["DISPLAY"] == ":0" and env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env is not layout["env"]


def test_a_missing_optional_path_is_skipped_not_an_error(layout):
    argv, _ = _wrap(layout, runner_dir=layout["home"] / "no-such-runner")
    assert str(layout["home"] / "no-such-runner") not in argv


def test_the_sandbox_dies_with_the_game_launcher(layout):
    assert "--die-with-parent" in _flags(_wrap(layout)[0])


def test_a_launcher_program_that_lives_in_the_home_is_bound_back(layout):
    """umu-run or a wine build in ~/.local/bin would vanish with the home otherwise."""
    tool = layout["home"] / ".local/bin/umu-run"
    tool.parent.mkdir(parents=True)
    tool.write_text("#!/bin/sh\n")
    argv, _ = _wrap(layout, argv=(str(tool), "game.exe"))
    assert (str(tool), str(tool)) in _pairs(_flags(argv), "--ro-bind")


def test_a_launcher_program_outside_the_home_is_left_alone(layout):
    argv, _ = _wrap(layout, argv=("/usr/bin/umu-run", "game.exe"))
    assert "/usr/bin/umu-run" not in _flags(argv)


def test_the_hidden_sockets_are_dropped_from_the_environment(layout):
    """umu's own container would try to bind the session bus it can no longer see."""
    layout["env"]["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={layout['runtime']}/bus"
    layout["env"]["SSH_AUTH_SOCK"] = str(layout["runtime"] / "keyring" / "ssh")
    _argv, env = _wrap(layout)
    assert "DBUS_SESSION_BUS_ADDRESS" not in env and "SSH_AUTH_SOCK" not in env
    assert "DBUS_SESSION_BUS_ADDRESS" in layout["env"]  # the caller's dict is untouched


def test_the_x_authority_file_is_bound_back_read_only(layout):
    cookie = layout["home"] / ".Xauthority"
    cookie.write_text("cookie")
    layout["env"]["XAUTHORITY"] = str(cookie)
    argv, env = _wrap(layout)
    assert (str(cookie), str(cookie)) in _pairs(_flags(argv), "--ro-bind")
    assert env["XAUTHORITY"] == str(cookie)


class _Done:
    def __init__(self, code=0, err=""):
        self.returncode = code
        self.stderr = err
        self.stdout = ""


def test_works_is_true_when_bwrap_starts(monkeypatch):
    monkeypatch.setattr(gamesandbox.subprocess, "run", lambda *a, **k: _Done(0))
    assert gamesandbox.works() == (True, "")


def test_works_reports_why_bwrap_cannot_start(monkeypatch):
    err = "bwrap: setting up uid map: Permission denied\n"
    monkeypatch.setattr(gamesandbox.subprocess, "run", lambda *a, **k: _Done(1, err))
    ok, why = gamesandbox.works()
    assert ok is False and "uid map" in why


def test_works_survives_a_missing_or_hanging_bwrap(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("bwrap")

    monkeypatch.setattr(gamesandbox.subprocess, "run", boom)
    assert gamesandbox.works()[0] is False

    def hang(*a, **k):
        raise gamesandbox.subprocess.TimeoutExpired("bwrap", 10)

    monkeypatch.setattr(gamesandbox.subprocess, "run", hang)
    assert gamesandbox.works()[0] is False


def test_the_gamescope_socket_stays_so_a_game_can_still_present(layout):
    """Found on real hardware: without it DXVK never made a swapchain and nothing rendered."""
    flags = _flags(_wrap(layout)[0])
    back = {Path(src).name for src, _ in _pairs(flags, "--bind")}
    assert "gamescope-0" in back
    assert not back & {"gamescope-0-lock", "wayland-0.lock"}


# ---- which bubblewrap: the Steam Runtime's own copy first, then the system's ---------------
def _runtime_copy(tmp_path, monkeypatch, name="steamrt4"):
    data = tmp_path / "xdg-data"
    srt = data / "umu" / name / "pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap"
    srt.parent.mkdir(parents=True)
    srt.write_text("#!/bin/sh\n")
    srt.chmod(0o755)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    return srt


def test_the_runtime_copy_is_found_where_umu_puts_it(tmp_path, monkeypatch):
    srt = _runtime_copy(tmp_path, monkeypatch)
    assert gamesandbox.runtime_copies() == [str(srt)]


def test_no_runtime_yet_means_no_runtime_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing"))
    assert gamesandbox.runtime_copies() == []


def test_a_working_runtime_copy_is_preferred_over_the_system_one(tmp_path, monkeypatch):
    srt = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    tried = []
    monkeypatch.setattr(
        gamesandbox, "works", lambda binary=gamesandbox.BINARY: tried.append(binary) or (True, "")
    )
    found = gamesandbox.locate()
    assert found.binary == str(srt) and found.source == "runtime" and found.found
    assert tried == [str(srt)], "the system copy need not even be tried"


def test_a_blocked_runtime_copy_falls_back_to_the_system_one(tmp_path, monkeypatch):
    srt = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: "/usr/bin/bwrap")

    def works(binary=gamesandbox.BINARY):
        if binary == str(srt):
            return False, "bwrap: setting up uid map: Permission denied"
        return True, ""

    monkeypatch.setattr(gamesandbox, "works", works)
    found = gamesandbox.locate()
    assert found.binary == "bwrap" and found.source == "system"


def test_with_only_the_system_copy_that_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing"))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(gamesandbox, "works", lambda binary=gamesandbox.BINARY: (True, ""))
    found = gamesandbox.locate()
    assert (found.binary, found.source) == ("bwrap", "system")


def test_when_every_copy_is_blocked_the_first_reason_is_kept(tmp_path, monkeypatch):
    _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    monkeypatch.setattr(
        gamesandbox, "works", lambda binary=gamesandbox.BINARY: (False, "setting up uid map")
    )
    found = gamesandbox.locate()
    assert found.binary is None and found.found and "uid map" in found.why


def test_when_there_is_no_copy_at_all_it_says_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing"))
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: None)
    found = gamesandbox.locate()
    assert found.binary is None and not found.found


def test_wrap_starts_whichever_bubblewrap_it_is_given(layout):
    argv, _ = _wrap(layout, bwrap="/home/u/.local/share/umu/steamrt4/srt-bwrap")
    assert argv[0] == "/home/u/.local/share/umu/steamrt4/srt-bwrap"


@pytest.mark.parametrize(
    ("family", "command"),
    [
        ("debian", "sudo apt install bubblewrap"),
        ("fedora", "sudo dnf install bubblewrap"),
        ("arch", "sudo pacman -S --needed bubblewrap"),
        ("suse", "sudo zypper install bubblewrap"),
    ],
)
def test_the_install_hint_names_the_right_command_for_the_distro(family, command):
    assert gamesandbox.install_hint(family) == command


def test_an_unknown_distro_still_gets_the_package_name():
    assert "bubblewrap" in gamesandbox.install_hint(None)


# ---- the runtime's bubblewrap is run outside the Sandbox: only copies Nadare recorded (#871) ----
def _another_copy(tmp_path, name, body="#!/bin/sh\n# planted\n"):
    srt = (
        tmp_path
        / "xdg-data"
        / "umu"
        / name
        / "pressure-vessel/libexec/steam-runtime-tools-0/srt-bwrap"
    )
    srt.parent.mkdir(parents=True)
    srt.write_text(body)
    srt.chmod(0o755)
    return srt


def _record_tried(monkeypatch, ok=True):
    tried: list[str] = []

    def works(binary=gamesandbox.BINARY):
        tried.append(binary)
        return (True, "") if ok else (False, "x")

    monkeypatch.setattr(gamesandbox, "works", works)
    return tried


def test_the_first_use_records_the_copies_that_are_there(tmp_path, monkeypatch):
    import hashlib
    import json

    srt = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: None)
    _record_tried(monkeypatch)
    assert gamesandbox.locate().binary == str(srt)
    pins = json.loads(gamesandbox.pins_path().read_text())
    assert pins == {str(srt): hashlib.sha256(srt.read_bytes()).hexdigest()}


def test_a_copy_that_appears_later_is_never_run(tmp_path, monkeypatch):
    old = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: None)
    tried = _record_tried(monkeypatch)
    assert gamesandbox.locate().binary == str(old)  # first use records it
    planted = _another_copy(tmp_path, "steamrt-zzz")
    os.utime(planted, (4_000_000_000, 4_000_000_000))  # the newest by mtime: what locate() prefers
    tried.clear()
    found = gamesandbox.locate()
    assert found.binary == str(old)
    assert str(planted) not in tried


def test_a_pinned_copy_whose_content_changed_is_not_run(tmp_path, monkeypatch):
    srt = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: None)
    tried = _record_tried(monkeypatch)
    gamesandbox.locate()
    srt.write_text("#!/bin/sh\n# replaced by the game\n")
    tried.clear()
    found = gamesandbox.locate()
    assert found.binary is None and tried == []


def test_only_unrecorded_copies_and_no_system_bubblewrap_says_what_to_do(tmp_path, monkeypatch):
    srt = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: None)
    _record_tried(monkeypatch)
    gamesandbox.locate()
    srt.unlink()  # umu replaced its runtime; the new copy is not one Nadare recorded
    _another_copy(tmp_path, "steamrt-new")
    found = gamesandbox.locate()
    assert found.binary is None and found.found
    assert "did not record" in found.why and str(gamesandbox.pins_path()) in found.why


def test_an_unrecorded_copy_gives_way_to_the_system_bubblewrap(tmp_path, monkeypatch):
    srt = _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: "/usr/bin/bwrap")
    tried = _record_tried(monkeypatch)
    gamesandbox.locate()
    srt.unlink()
    planted = _another_copy(tmp_path, "steamrt-new")
    tried.clear()
    found = gamesandbox.locate()
    assert found.binary == "bwrap" and found.source == "system"
    assert str(planted) not in tried


def test_a_damaged_record_trusts_nothing_instead_of_everything(tmp_path, monkeypatch):
    _runtime_copy(tmp_path, monkeypatch)
    monkeypatch.setattr(gamesandbox.shutil, "which", lambda name: None)
    tried = _record_tried(monkeypatch)
    gamesandbox.pins_path().parent.mkdir(parents=True, exist_ok=True)
    gamesandbox.pins_path().write_text("{ not json")
    assert gamesandbox.locate().binary is None and tried == []


# ---- a bind that would give the hidden home back is refused (#850) -----------------------------
def test_a_prefix_that_is_the_home_folder_is_refused(layout):
    layout["prefix"] = layout["home"]
    with pytest.raises(gamesandbox.BroadBindError, match="home folder"):
        _wrap(layout)


def test_a_game_folder_that_holds_the_home_folder_is_refused(layout):
    with pytest.raises(gamesandbox.BroadBindError):
        _wrap(layout, install_dir=layout["home"].parent)


def test_a_game_folder_that_is_a_whole_drive_is_refused(layout, monkeypatch):
    drive = layout["home"].parent / "drive"
    drive.mkdir()
    real = os.path.ismount
    monkeypatch.setattr(os.path, "ismount", lambda p: str(p) == str(drive.resolve()) or real(p))
    with pytest.raises(gamesandbox.BroadBindError, match="drive"):
        _wrap(layout, install_dir=drive)


def test_ordinary_game_folders_are_still_bound(layout):
    argv, _env = _wrap(layout)
    assert argv[0] == "bwrap"
