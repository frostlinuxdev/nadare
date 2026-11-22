# SPDX-License-Identifier: GPL-3.0-or-later
# ruff: noqa: F811
"""Regression tests for the Codex safety review, with real filesystem and signed fixtures."""

import json
import threading
from types import SimpleNamespace

from test_release_signature import sign
from test_safety_check import _doc, _indicator, signed_feed  # noqa: F401

from nadare.core import safetycheck as s


def rules(kind="file_contains", glob="sub/example.log"):
    rule = {"type": kind, "glob": glob}
    if kind == "file_contains":
        rule["text"] = "marker"
    return s.parse_indicators(_doc(_indicator(rule=rule)))[0]


def test_parent_swap_cannot_read_outside(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    sub = prefix / "sub"
    sub.mkdir(parents=True)
    (sub / "example.log").write_text("safely")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "example.log").write_text("marker")
    real_entries = s._entries

    def swapped(*args):
        result = real_entries(*args)
        sub.rename(prefix / "original")
        sub.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(s, "_entries", swapped)
    result = s.check_profile("p", {"prefix": str(prefix)}, rules())
    # The swapped folder is refused: nothing read, and the check says it is incomplete.
    assert result.bytes_read == 0, result
    assert result.level == "incomplete"


def test_a_real_folder_moved_into_place_is_refused(tmp_path, monkeypatch):
    # Not a link this time: an outside folder renamed over the scanned one. Only the
    # folder identity check (device + inode) can tell the two apart.
    prefix = tmp_path / "prefix"
    sub = prefix / "sub"
    sub.mkdir(parents=True)
    (sub / "example.log").write_text("safely")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "example.log").write_text("marker")
    real_entries = s._entries

    def swapped(*args):
        result = real_entries(*args)
        sub.rename(prefix / "original")
        outside.rename(sub)
        return result

    monkeypatch.setattr(s, "_entries", swapped)
    result = s.check_profile("p", {"prefix": str(prefix)}, rules())
    assert result.bytes_read == 0, result
    assert result.level == "incomplete"


def test_directory_swap_cannot_enumerate_outside(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    sub = prefix / "sub"
    sub.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "bad.example").write_text("")
    real_open = s.os.open

    def swapped(path, flags, *args, **kwargs):
        if str(path) in (str(sub), "sub"):
            sub.rename(prefix / "original")
            sub.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    real_scandir = s.os.scandir

    def scanned(path):
        if str(path) == str(sub):
            sub.rename(prefix / "original")
            sub.symlink_to(outside, target_is_directory=True)
        return real_scandir(path)

    monkeypatch.setattr(s.os, "open", swapped)
    monkeypatch.setattr(s.os, "scandir", scanned)
    result = s.check_profile("p", {"prefix": str(prefix)}, rules("path_exists", "sub/bad.example"))
    assert result.level != "exposed", result


def test_entry_cap_bounds_enumeration(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    for i in range(100):
        (prefix / str(i)).touch()
    real_scandir = s.os.scandir
    seen = []

    def counted(path):
        with real_scandir(path) as entries:
            for entry in entries:
                seen.append(entry.name)
                yield entry

    class Scan:
        def __init__(self, path):
            self.entries = counted(path)

        def __iter__(self):
            return self.entries

        def __enter__(self):
            return self.entries

        def __exit__(self, *args):
            self.entries.close()

    monkeypatch.setattr(s.os, "scandir", Scan)
    s.check_profile("p", {"prefix": str(prefix)}, rules(), max_files=1)
    assert len(seen) <= 2, len(seen)


def test_exhausted_byte_budget_does_not_claim_clean(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "example.log").write_text("xxxxmarker")
    result = s.check_profile(
        "p", {"prefix": str(prefix)}, rules(glob="example.log"), max_total_bytes=1
    )
    assert result.level != "clean", (result, s.result_summary(result))


def test_unvisited_files_do_not_claim_clean(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "bad.example").touch()
    result = s.check_profile(
        "p", {"prefix": str(prefix)}, rules("path_exists", "bad.example"), max_files=0
    )
    assert result.level != "clean", (result, s.result_summary(result))


def test_concurrent_accept_cannot_lower_watermark(signed_feed, monkeypatch):
    feed = signed_feed
    ready = threading.Event()
    release = threading.Event()

    def opener(serial):
        body = json.dumps(_doc(_indicator(id=f"serial-{serial}"), serial=serial)).encode()
        signature = sign(body, feed["key"], feed["fingerprint"]).encode()

        def fetch(url):
            if serial == 1 and not url.endswith(".asc"):
                ready.set()
                assert release.wait(5)
            return signature if url.endswith(".asc") else body

        return fetch

    outcome = []
    thread = threading.Thread(
        name="older-loader",
        target=lambda: outcome.append(s.load_indicators(cache=feed["cache"], opener=opener(1))),
    )
    thread.start()
    try:
        assert ready.wait(5)
        assert s.load_indicators(cache=feed["cache"], opener=opener(2))[0].id == "serial-2"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert json.loads(feed["serial"].read_text())["serial"] == 2
    assert outcome[0][0].id != "serial-1"


def test_old_task_completion_does_not_forget_current_task():
    from nadare.gui.main_window import MainWindow

    current = object()
    window = SimpleNamespace(_safety_task=current, _current_profile="new-profile")
    MainWindow._on_safety_done(window, "old-profile", s.SafetyResult(level=s.CANCELLED))
    assert window._safety_task is current


def test_accepting_saved_list_establishes_watermark(signed_feed):
    feed = signed_feed
    body = json.dumps(_doc(_indicator(id="newer"), serial=2)).encode()
    signature = sign(body, feed["key"], feed["fingerprint"])
    feed["cache"].write_text(json.dumps({"body": body.decode(), "signature": signature}))
    feed["error"] = OSError("offline")
    assert s.load_indicators(cache=feed["cache"])[0].id == "newer"
    feed["error"] = None
    feed["doc"] = _doc(_indicator(id="older"), serial=1)
    result = s.load_indicators(cache=feed["cache"])
    assert result[0].id == "newer"


def test_watermark_write_failure_does_not_accept_unprotected_list(signed_feed, monkeypatch):
    real_write = s.atomicfile.write

    def failed(path, *args, **kwargs):
        if path == signed_feed["serial"]:
            raise OSError("fixture cannot save watermark")
        return real_write(path, *args, **kwargs)

    monkeypatch.setattr(s.atomicfile, "write", failed)
    assert s.load_indicators(cache=signed_feed["cache"]) is None
    assert not signed_feed["cache"].exists()


def test_missing_root_is_incomplete(tmp_path):
    result = s.check_profile("p", {"prefix": str(tmp_path / "missing")}, rules())
    assert result.level == "incomplete"
    assert result.truncated


def test_incomplete_result_is_saved_without_losing_compromise_warning(tmp_path):
    store = tmp_path / "results.json"
    s.save_result("p", s.SafetyResult(level=s.COMPROMISED), path=store)
    s.save_result("p", s.SafetyResult(level="incomplete", truncated=True), path=store)
    result = s.load_result("p", path=store)
    assert result is not None and result.level == "incomplete"
    assert result.compromise_warning
    assert "Incomplete" in s.system_check_line(path=store)
    assert "previous" in s.result_summary(result).lower()


def test_incomplete_positive_match_still_reports_skipped_files(tmp_path):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    (prefix / "example.log").write_text("marker extra")
    result = s.check_profile(
        "p", {"prefix": str(prefix)}, rules(glob="example.log"), max_total_bytes=6
    )
    assert result.level == "exposed"
    assert result.truncated
    assert "incomplete" in s.result_summary(result).lower()


def test_acceptance_is_serialized_between_processes(signed_feed, monkeypatch):
    import multiprocessing

    ctx = multiprocessing.get_context("fork")
    ready, release, newer_done = (ctx.Event() for _ in range(3))
    real_accepted = s._serial_accepted

    def paused(watermark, serial, digest):
        accepted = real_accepted(watermark, serial, digest)
        if serial == 1:
            ready.set()
            assert release.wait(5)
        return accepted

    monkeypatch.setattr(s, "_serial_accepted", paused)

    def load(serial):
        body = json.dumps(_doc(_indicator(id=f"serial-{serial}"), serial=serial)).encode()
        signature = sign(body, signed_feed["key"], signed_feed["fingerprint"]).encode()
        s.load_indicators(
            cache=signed_feed["cache"],
            opener=lambda url: signature if url.endswith(".asc") else body,
        )
        if serial == 2:
            newer_done.set()

    older = ctx.Process(target=load, args=(1,))
    newer = ctx.Process(target=load, args=(2,))
    older.start()
    try:
        assert ready.wait(5)
        newer.start()
        completed_while_older_accepts = newer_done.wait(0.3)
    finally:
        release.set()
        older.join(5)
        if newer.pid is not None:
            newer.join(5)
        for process in (older, newer):
            if process.is_alive():
                process.terminate()
                process.join(5)
    assert older.exitcode == newer.exitcode == 0
    assert not completed_while_older_accepts
    assert json.loads(signed_feed["serial"].read_text())["serial"] == 2


def test_failed_read_still_consumes_byte_budget(tmp_path, monkeypatch):
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    for name in ("a.log", "b.log"):
        (prefix / name).write_bytes(b"x" * (2 << 20))
    real_read = s.os.read
    consumed = 0
    calls = 0

    def interrupted(fd, count):
        nonlocal consumed, calls
        calls += 1
        if calls == 2:
            raise OSError("fixture read failure after one chunk")
        chunk = real_read(fd, count)
        consumed += len(chunk)
        return chunk

    monkeypatch.setattr(s.os, "read", interrupted)
    cap = 3 * (1 << 19)
    result = s.check_profile("p", {"prefix": str(prefix)}, rules(glob="*.log"), max_total_bytes=cap)
    assert consumed <= cap
    assert result.bytes_read == consumed
    assert result.level == "incomplete"


def test_a_prefix_with_more_folders_than_open_files_still_completes(tmp_path):
    # Desktop sessions often start with a soft limit of 1024 open files; a Wine prefix
    # has thousands of folders. Holding one descriptor per folder must not make every
    # real prefix "incomplete". Run under a low limit in a child process.
    import subprocess
    import sys

    prefix = tmp_path / "pfx"
    for i in range(300):
        (prefix / f"d{i:03}" / "inner").mkdir(parents=True)
    (prefix / "d299" / "inner" / "bad.example").write_text("x")
    code = f"""
import resource
resource.setrlimit(resource.RLIMIT_NOFILE, (128, resource.getrlimit(resource.RLIMIT_NOFILE)[1]))
from nadare.core import safetycheck as s
rule = s.Indicator("fake", "Example", ("*",), "exposed",
                   {{"type": "path_exists", "glob": "d299/inner/bad.example"}})
r = s.check_profile("p", {{"prefix": {str(prefix)!r}}}, [rule])
print(r.level, r.truncated)
"""
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["exposed", "False"]
