from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from nadare.core import sevenzip


def test_system_candidates_are_preferred_in_order(tmp_path):
    bundled = tmp_path / "bundled"
    bundled.write_bytes(b"unused")
    for expected, choices, seen in [("/system/7z", {"7z": "/system/7z"}, ["7z"]),
                                    ("/system/7zz", {"7zz": "/system/7zz"}, ["7z", "7zz"]),
                                    ("/system/7za", {"7za": "/system/7za"}, ["7z", "7zz", "7za"])]:
        calls = []
        def fake_which(name, choices=choices, calls=calls):
            calls.append(name)
            return choices.get(name)

        assert sevenzip.find(which=fake_which,
                             bundled=bundled, expected_sha256="bad") == expected
        assert calls == seen


def test_bundled_binary_requires_digest_and_is_cached(tmp_path, monkeypatch):
    src = tmp_path / "7zzs"
    data = b"trusted test executable"
    src.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    cache = tmp_path / "cache"
    assert sevenzip.find(which=lambda _: None, bundled=src, expected_sha256=digest,
                         cache_dir=lambda: cache) is not None
    expected = cache / "bin" / f"7zzs-{digest[:12]}"
    assert expected.stat().st_mode & 0o777 == 0o755
    src.chmod(0o755)
    # Test the copy path directly with an injected exec failure.
    result = sevenzip.find(which=lambda _: None, bundled=src, expected_sha256=digest,
                           cache_dir=lambda: cache,
                           run=lambda *a, **k: (_ for _ in ()).throw(OSError("noexec")))
    assert result == str(expected) and expected.read_bytes() == data
    assert expected.stat().st_mode & 0o777 == 0o755
    def noexec(*args, **kwargs):
        raise OSError("noexec")

    assert sevenzip.find(which=lambda _: None, bundled=src, expected_sha256=digest,
                         cache_dir=lambda: cache, run=noexec) == str(expected)
    expected.write_bytes(b"tampered")
    assert sevenzip.find(which=lambda _: None, bundled=src, expected_sha256=digest,
                         cache_dir=lambda: cache, run=noexec) == str(expected)
    assert hashlib.sha256(expected.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize("data", [b"bad", b""])
def test_bad_bundled_digest_is_not_copied(tmp_path, monkeypatch, data):
    src = tmp_path / "7zzs"
    src.write_bytes(data)
    monkeypatch.setattr(sevenzip.paths, "cache_dir", lambda: tmp_path / "cache")
    assert sevenzip.find(which=lambda _: None, bundled=src, expected_sha256="0" * 64) is None
    assert not (tmp_path / "cache").exists()


@pytest.mark.skipif(not Path("src/nadare/assets/7zzs").is_file(), reason="bundled 7zzs absent")
def test_real_bundled_binary_smoke(tmp_path):
    binary = Path("src/nadare/assets/7zzs").resolve()
    payload = tmp_path / "payload.txt"
    payload.write_text("7zip smoke")
    archive = tmp_path / "tiny.7z"
    subprocess.run([str(binary), "a", str(archive), str(payload)], check=True, capture_output=True)
    assert "payload.txt" in subprocess.run([str(binary), "l", str(archive)], check=True,
                                           capture_output=True, text=True).stdout
    out = tmp_path / "out"
    subprocess.run([str(binary), "x", "-y", f"-o{out}", str(archive)], check=True,
                   capture_output=True)
    assert (out / "payload.txt").read_text() == "7zip smoke"


def test_executable_file_with_a_wrong_digest_is_refused_and_never_run(tmp_path):
    fake = tmp_path / "7zzs"
    fake.write_bytes(b"this is not 7-Zip")
    fake.chmod(0o755)
    ran = []
    found = sevenzip.find(
        which=lambda _name: None,
        bundled=fake,
        cache_dir=lambda: tmp_path / "cache",
        run=lambda *args, **kwargs: ran.append(args),
    )
    assert found is None
    assert ran == []  # an unverified file must not even be probed
    assert not (tmp_path / "cache").exists()


def test_default_bundled_path_is_looked_up_at_call_time(tmp_path, monkeypatch):
    payload = b"stand-in for 7zzs"
    stand_in = tmp_path / "7zzs"
    stand_in.write_bytes(payload)
    stand_in.chmod(0o755)
    monkeypatch.setattr(sevenzip, "BUNDLED", stand_in)
    digest = hashlib.sha256(payload).hexdigest()
    found = sevenzip.find(
        which=lambda _name: None,
        expected_sha256=digest,
        cache_dir=lambda: tmp_path / "cache",
        run=lambda *args, **kwargs: None,
    )
    assert found == str(stand_in)


def test_missing_bundled_file_means_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(sevenzip, "BUNDLED", tmp_path / "nope")
    assert sevenzip.find(which=lambda _name: None) is None
