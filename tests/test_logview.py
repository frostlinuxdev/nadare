# SPDX-License-Identifier: GPL-3.0-or-later
"""Log listing, tailing, session splitting, trimming and redaction for the in-app log viewer."""

from __future__ import annotations

import os

from nadare.core import logview

HEADER = "===== 2026-10-26T10:00:00 {op} ({profile}) | nadare 0.1.0b12 =====\n"


def write(directory, name, text, mtime=None):
    path = directory / name
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_list_logs_parses_profile_and_operation_and_sorts_newest_first(tmp_path):
    write(tmp_path, "CZN-launch.log", "a", 100)
    write(tmp_path, "CZN-FRESH-launch.log", "b", 300)
    write(tmp_path, "Arknights-install.log", "c", 200)
    write(tmp_path, "nadare.log", "app", 400)
    logs = logview.list_logs(["CZN", "CZN-FRESH", "Arknights"], tmp_path)
    assert [(f.profile, f.operation) for f in logs] == [
        (None, "app"),
        ("CZN-FRESH", "launch"),
        ("Arknights", "install"),
        ("CZN", "launch"),
    ]


def test_list_logs_filtered_to_one_profile_never_mixes_a_longer_profile_name(tmp_path):
    write(tmp_path, "CZN-launch.log", "a")
    write(tmp_path, "CZN-FRESH-launch.log", "b")
    only = logview.list_logs(["CZN", "CZN-FRESH"], tmp_path, profile="CZN")
    assert [f.path.name for f in only] == ["CZN-launch.log"]


def test_list_logs_includes_rotated_app_logs_and_skips_other_files(tmp_path):
    write(tmp_path, "nadare.log", "x")
    write(tmp_path, "nadare.log.1", "y")
    write(tmp_path, "notes.txt", "z")
    (tmp_path / "sub").mkdir()
    names = {f.path.name for f in logview.list_logs([], tmp_path)}
    assert names == {"nadare.log", "nadare.log.1"}


def test_list_logs_unknown_profile_files_still_listed_with_best_guess(tmp_path):
    write(tmp_path, "mystery-install.log", "x")
    (log,) = logview.list_logs([], tmp_path)
    assert (log.profile, log.operation) == ("mystery", "install")


def test_list_logs_missing_directory_is_empty(tmp_path):
    assert logview.list_logs([], tmp_path / "nope") == []


def test_read_tail_returns_everything_when_small(tmp_path):
    path = write(tmp_path, "a.log", "one\ntwo\n")
    assert logview.read_tail(path) == "one\ntwo\n"


def test_read_tail_cuts_to_whole_lines_and_says_so(tmp_path):
    path = write(tmp_path, "a.log", "".join(f"line {i}\n" for i in range(1000)))
    text = logview.read_tail(path, max_bytes=200)
    assert text.startswith("(older lines not shown)\n")
    body = text.split("\n", 1)[1]
    assert body.endswith("line 999\n") and body.startswith("line ")
    assert len(body) <= 200


def test_read_tail_handles_missing_files_and_bad_utf8(tmp_path):
    assert logview.read_tail(tmp_path / "gone.log") == ""
    path = tmp_path / "b.log"
    path.write_bytes(b"ok \xff\xfe bytes\n")
    assert "ok" in logview.read_tail(path)


def test_sessions_split_at_headers_and_keep_leading_text():
    text = (
        "orphan\n"
        + HEADER.format(op="install", profile="X")
        + "a\n"
        + HEADER.format(op="install", profile="X")
        + "b\n"
    )
    parts = logview.sessions(text)
    assert len(parts) == 3
    assert parts[0] == "orphan\n" and parts[-1].endswith("b\n") and parts[-1].startswith("=====")


def test_sessions_of_plain_text_is_one_session():
    assert logview.sessions("a\nb\n") == ["a\nb\n"]
    assert logview.sessions("") == []


def test_filter_lines_is_case_insensitive_by_default():
    text = "Error: boom\nall fine\nerror again\n"
    assert logview.filter_lines(text, "ERROR") == "Error: boom\nerror again\n"
    assert logview.filter_lines(text, "ERROR", case=True) == ""
    assert logview.filter_lines(text, "") == text


def test_trim_sessions_drops_oldest_sessions_but_keeps_the_newest(tmp_path):
    blocks = [
        HEADER.format(op="install", profile="X") + f"run {i}\n" + "x" * 200 + "\n"
        for i in range(10)
    ]
    path = write(tmp_path, "X-install.log", "".join(blocks))
    assert logview.trim_sessions(path, max_bytes=700) is True
    left = path.read_text(encoding="utf-8")
    assert len(left.encode()) <= 700 and "run 9" in left and "run 0" not in left
    assert left.startswith("=====")


def test_trim_sessions_leaves_small_files_alone_and_never_empties_the_file(tmp_path):
    small = write(tmp_path, "s.log", HEADER.format(op="a", profile="X") + "tiny\n")
    assert logview.trim_sessions(small, max_bytes=10_000) is False
    huge = write(tmp_path, "h.log", HEADER.format(op="a", profile="X") + "y" * 5000 + "\n")
    assert logview.trim_sessions(huge, max_bytes=100) is False
    assert "y" * 5000 in huge.read_text(encoding="utf-8")


def test_redacted_hides_the_home_path_and_emails():
    home = os.path.expanduser("~")
    out = logview.redacted(f"opened {home}/games/x by me@example.com\n")
    assert home not in out and "me@example.com" not in out and "~/games/x" in out


def test_redacted_keeps_timestamps_and_paths_readable_but_masks_known_secrets():
    text = (
        "2026-10-26T13:52:05 launch /opt/games/Some-Long-Directory-Name/Game.exe\n"
        "password=hunter2hunter2 and https://user:pw@host.example/x\n"
        "authorization: Bearer abcdefghijklmnop\n"
    )
    out = logview.redacted(text)
    assert "2026-10-26T13:52:05" in out
    assert "/opt/games/Some-Long-Directory-Name/Game.exe" in out
    assert "hunter2hunter2" not in out and "user:pw@" not in out
    assert "abcdefghijklmnop" not in out


# ---- b14: one zip with every log, for bug reports ---------------------------------------
def _zip_names(path):
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


def test_bundle_holds_every_listed_log_redacted_and_a_readme(tmp_path):
    home = os.path.expanduser("~")
    logs = tmp_path / "logs"
    logs.mkdir()
    write(logs, "G-launch.log", f"crash at {home}/game\n", 100)
    write(logs, "nadare.log", "app line\n", 200)
    out = tmp_path / "out" / "bundle.zip"
    count = logview.bundle(["G"], logs, out)
    files = _zip_names(out)
    assert count == 2
    assert set(files) == {"README.txt", "G-launch.log", "nadare.log"}
    assert home not in files["G-launch.log"] and "~/game" in files["G-launch.log"]
    assert "2 log files" in files["README.txt"] and "hidden" in files["README.txt"]


def test_bundle_without_redaction_keeps_the_text_and_says_so(tmp_path):
    home = os.path.expanduser("~")
    logs = tmp_path / "logs"
    logs.mkdir()
    write(logs, "G-launch.log", f"at {home}/x\n")
    out = tmp_path / "b.zip"
    logview.bundle(["G"], logs, out, redact=False)
    files = _zip_names(out)
    assert f"{home}/x" in files["G-launch.log"]
    assert "NOT hidden" in files["README.txt"]


def test_bundle_can_be_limited_to_one_profile_and_caps_each_file(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    write(logs, "G-install.log", "x" * 5000 + "\nlast line\n")
    write(logs, "H-install.log", "other\n")
    write(logs, "nadare.log", "app\n")
    out = tmp_path / "b.zip"
    count = logview.bundle(["G", "H"], logs, out, profile="G", max_bytes_per_file=1000)
    files = _zip_names(out)
    assert count == 1 and set(files) == {"README.txt", "G-install.log"}
    assert "last line" in files["G-install.log"] and len(files["G-install.log"]) < 1500


def test_bundle_with_no_logs_still_writes_a_readme_and_no_partial_file_on_error(
    tmp_path, monkeypatch
):
    out = tmp_path / "empty.zip"
    assert logview.bundle([], tmp_path / "missing", out) == 0
    assert set(_zip_names(out)) == {"README.txt"}

    logs = tmp_path / "logs"
    logs.mkdir()
    write(logs, "G-x.log", "a\n")
    broken = tmp_path / "broken.zip"

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(logview, "read_tail", boom)
    try:
        logview.bundle(["G"], logs, broken)
    except OSError:
        pass
    assert not broken.exists() and not list(tmp_path.glob("broken.zip*"))
