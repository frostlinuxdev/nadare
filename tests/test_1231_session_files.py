# SPDX-License-Identifier: GPL-3.0-or-later
"""#1231: a sign-in session file left by an interrupted run is named, never shared."""

from __future__ import annotations

import os
from pathlib import Path

from nadare.core import sessionfiles
from nadare.core.report import build_report

STAGE = ("drive_c", "Games", ".nadare", "nikke")


def _profiles(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "profiles"
    prefix = tmp_path / "games" / "NIKKE" / "prefix"
    return root, {
        "czn": {"backend": "stove-czn", "title": "CZN"},
        "nikke": {"backend": "launcher-nikke", "title": "NIKKE", "prefix": str(prefix)},
    }


def _touch(path: Path, text: str = "{}") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_nothing_left_means_nothing_found(tmp_path):
    root, profiles = _profiles(tmp_path)
    assert sessionfiles.find_leftovers(profiles, root) == []


def test_czn_stub_and_auth_sessions_are_found(tmp_path):
    root, profiles = _profiles(tmp_path)
    stub = _touch(root / "czn" / "stub" / "session.json")
    auth = _touch(root / "czn" / "auth" / "session.json")
    bak = _touch(root / "czn" / "auth" / "session.json.expired-bak")
    found = sessionfiles.find_leftovers(profiles, root)
    assert sorted(found) == sorted([stub, auth, bak])


def test_a_nikke_session_in_the_prefix_is_found(tmp_path):
    root, profiles = _profiles(tmp_path)
    stage = Path(profiles["nikke"]["prefix"]).joinpath(*STAGE)
    plain = _touch(stage / "session-abc123" / "session.json")
    enc = _touch(stage / "session-abc123" / "session.enc.json")
    assert sorted(sessionfiles.find_leftovers(profiles, root)) == sorted([plain, enc])


def test_other_files_and_links_are_not_reported(tmp_path):
    root, profiles = _profiles(tmp_path)
    _touch(root / "czn" / "stub" / "czn_pcsdk_stub.exe", "x")
    _touch(root / "czn" / "auth" / "czn_auth.json")
    outside = _touch(tmp_path / "elsewhere" / "session.json")
    link = root / "czn" / "stub" / "session.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, link)
    assert sessionfiles.find_leftovers(profiles, root) == []


def test_an_odd_profile_entry_does_not_break_the_search(tmp_path):
    root, _ = _profiles(tmp_path)
    found = sessionfiles.find_leftovers(
        {"x": "not a dict", "../escape": {}, "y": {"prefix": 5}}, root
    )
    assert found == []


def _report(tmp_path, profiles, root):
    return build_report(
        {"active_profile": "", "profiles": profiles},
        logs_path=tmp_path / "logs",
        profiles_path=root,
        username="fakeuser",
        home=tmp_path / "home",
        hostname="fakehost",
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
    )


def test_the_report_warns_about_a_leftover_without_including_it(tmp_path):
    root, profiles = _profiles(tmp_path)
    _touch(root / "czn" / "stub" / "session.json", '{"token":"SECRET-TOKEN-VALUE"}')
    report = _report(tmp_path, profiles, root)
    assert "SECRET-TOKEN-VALUE" not in report
    assert "session file" in report.lower()
    assert "delete" in report.lower()
    # Counted, not located: the path would name the game.
    section = report.split("## Session files", 1)[1].split("\n## ", 1)[0]
    assert "1" in section
    assert "czn" not in section.lower() and "stub" not in section


def test_a_clean_report_has_no_warning(tmp_path):
    root, profiles = _profiles(tmp_path)
    assert "session file" not in _report(tmp_path, profiles, root).lower()


def test_the_warning_survives_truncation(tmp_path):
    root, profiles = _profiles(tmp_path)
    _touch(root / "czn" / "stub" / "session.json")
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "nadare.log").write_text("noise\n" * 200_000, encoding="utf-8")
    report = build_report(
        {"active_profile": "", "profiles": profiles},
        logs_path=logs,
        profiles_path=root,
        run_command=lambda *a, **k: "unavailable",
        proc_root=tmp_path / "proc",
        max_bytes=1_500,
    )
    assert "truncated" in report
    assert "session file" in report.lower()


# ---- NIKKE: a killed run's session folder goes at the next run ------------------------------


def test_a_new_nikke_run_clears_a_killed_runs_session_folder(tmp_path):
    from nadare.backends.nikke import hosts

    stage = tmp_path / "stage"
    old = _touch(stage / "session-old" / "session.json")
    keep = _touch(stage / "sail_host.exe", "x")
    work = hosts.private_workdir(stage)
    assert work.is_dir() and work.name.startswith("session-")
    assert not old.exists() and not old.parent.exists()
    assert keep.exists()


def test_clearing_never_follows_a_link_named_like_a_session_folder(tmp_path):
    from nadare.backends.nikke import hosts

    stage = tmp_path / "stage"
    stage.mkdir()
    victim = _touch(tmp_path / "victim" / "session.json", "precious")
    os.symlink(victim.parent, stage / "session-link")
    hosts.private_workdir(stage)
    assert victim.read_text(encoding="utf-8") == "precious"


def test_only_a_runs_session_folder_counts_in_the_nikke_stage(tmp_path):
    root, profiles = _profiles(tmp_path)
    stage = Path(profiles["nikke"]["prefix"]).joinpath(*STAGE)
    _touch(stage / "helpers" / "session.json")
    assert sessionfiles.find_leftovers(profiles, root) == []


def test_a_linked_folder_on_the_way_to_the_stage_is_not_followed(tmp_path):
    root, profiles = _profiles(tmp_path)
    prefix = Path(profiles["nikke"]["prefix"])
    elsewhere = tmp_path / "elsewhere"
    _touch(elsewhere / ".nadare" / "nikke" / "session-x" / "session.json")
    (prefix / "drive_c").mkdir(parents=True)
    os.symlink(elsewhere, prefix / "drive_c" / "Games")
    assert sessionfiles.find_leftovers(profiles, root) == []


def test_a_profile_name_that_leaves_the_profiles_folder_is_ignored(tmp_path):
    root, _ = _profiles(tmp_path)
    root.mkdir(parents=True)
    _touch(tmp_path / "escape" / "stub" / "session.json")
    assert sessionfiles.find_leftovers({"../escape": {}}, root) == []


def test_a_link_named_like_a_session_folder_is_removed_not_followed(tmp_path):
    from nadare.backends.nikke import hosts

    stage = tmp_path / "stage"
    stage.mkdir()
    victim = _touch(tmp_path / "victim" / "session.json", "precious")
    os.symlink(victim.parent, stage / "session-link")
    hosts.private_workdir(stage)
    assert not (stage / "session-link").is_symlink()
    assert victim.exists()
