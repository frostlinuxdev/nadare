# SPDX-License-Identifier: GPL-3.0-or-later
"""The "rail_setup" repository format (Tencent's Jupiter launcher CDN), read without its code.

NIKKE's launcher is published as a repository: a QQTEA-wrapped manifest (zstd + protobuf, file
names AES-encrypted) and content-addressed chunks. Nadare downloads it itself so no publisher
installer has to run (#949). Every byte is checked against the manifest's MD5s.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
from _rail_fixtures import PI_KEY, Repo, lz4_literals_only, lz4_with_match, md5, qqtea_encrypt

from nadare.core import railrepo
from nadare.core.events import Cancel, Cancelled

FILES = {
    "nikke_launcher.exe": b"MZ" + bytes(range(256)) * 5,
    "tpf_ui": None,
    "tpf_ui\\skin\\a.png": b"\x89PNG" + b"x" * 40,
    "intl_service\\INTLRuntime.dll": b"dll-bytes-" * 9,
    "empty.dat": b"",
}


def _repos(repo: Repo) -> list[railrepo.Repository]:
    return railrepo.repositories(repo.version_info())


def _install(repo: Repo, dest: Path, **kwargs) -> railrepo.InstallResult:
    return railrepo.install(_repos(repo), dest, fetch=repo.fetch, **kwargs)


def _tree(root: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".nadare-" not in str(path.relative_to(root)):
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


# ---- QQTEA ---------------------------------------------------------------------------------
@pytest.mark.parametrize("size", [0, 1, 7, 8, 15, 16, 100, 4097])
def test_qqtea_round_trips(size):
    plain = os.urandom(size)
    assert railrepo.qqtea_decrypt(qqtea_encrypt(plain)) == plain


def test_qqtea_rejects_a_wrong_key_and_bad_lengths():
    data = qqtea_encrypt(b"hello rail")
    with pytest.raises(railrepo.RailError):
        railrepo.qqtea_decrypt(data, b"0123456789abcdef")
    with pytest.raises(railrepo.RailError):
        railrepo.qqtea_decrypt(data[:-3])
    with pytest.raises(railrepo.RailError):
        railrepo.qqtea_decrypt(b"")
    assert railrepo.PI_KEY == PI_KEY


# ---- LZ4 -------------------------------------------------------------------------------------
def test_lz4_literals_and_matches():
    raw = os.urandom(300)
    assert railrepo.lz4_block_decompress(lz4_literals_only(raw), len(raw)) == raw
    block, raw = lz4_with_match(b"abcdefgh", 300)
    assert railrepo.lz4_block_decompress(block, len(raw)) == raw
    block, raw = lz4_with_match(b"z", 40)  # overlapping copy (offset 1)
    assert railrepo.lz4_block_decompress(block, len(raw)) == raw


@pytest.mark.parametrize(
    "block",
    [
        b"\x50abc",  # literal run longer than the data
        b"\x44abcd\x09\x00",  # offset beyond what was written
        b"\x44abcd\x00\x00",  # offset zero
    ],
)
def test_lz4_rejects_corrupt_blocks(block):
    with pytest.raises(railrepo.RailError):
        railrepo.lz4_block_decompress(block, 64)


def test_lz4_rejects_output_of_the_wrong_size():
    raw = b"0123456789"
    with pytest.raises(railrepo.RailError):
        railrepo.lz4_block_decompress(lz4_literals_only(raw), 9)


# ---- manifest --------------------------------------------------------------------------------
def test_the_manifest_lists_paths_sizes_and_chunks():
    repo = Repo(FILES)
    (entry_repo,) = _repos(repo)
    entries = railrepo.parse_manifest(repo.manifest, entry_repo)
    assert [e.path for e in entries] == list(FILES)
    by_path = {e.path: e for e in entries}
    assert by_path["tpf_ui"].is_dir and by_path["tpf_ui"].chunks == ()
    exe = by_path["nikke_launcher.exe"]
    assert exe.size == len(FILES["nikke_launcher.exe"])
    assert exe.md5 == md5(FILES["nikke_launcher.exe"])
    assert sum(c.size for c in exe.chunks) == exe.size
    assert {c.method for c in exe.chunks} == {0, 1, 2, 3}


def test_a_manifest_for_another_repository_is_refused():
    repo = Repo(FILES)
    (entry_repo,) = _repos(repo)
    other = railrepo.Repository(**{**entry_repo.__dict__, "repository_id": 49})
    with pytest.raises(railrepo.RailError):
        railrepo.parse_manifest(repo.manifest, other)


def test_a_wrong_name_key_is_refused():
    repo = Repo(FILES)
    (entry_repo,) = _repos(repo)
    other = railrepo.Repository(**{**entry_repo.__dict__, "key_hex": "ff" * 16})
    with pytest.raises(railrepo.RailError):
        railrepo.parse_manifest(repo.manifest, other)


def test_garbage_is_a_rail_error_not_a_crash():
    repo = Repo(FILES)
    (entry_repo,) = _repos(repo)
    for data in (b"", os.urandom(64), qqtea_encrypt(b"\xff" * 40), qqtea_encrypt(b"")):
        with pytest.raises(railrepo.RailError):
            railrepo.parse_manifest(data, entry_repo)


def test_repositories_reads_the_api_record():
    repo = Repo(FILES)
    (entry,) = railrepo.repositories(repo.version_info())
    assert entry.repository_id == 50
    assert entry.cdn_root == repo.cdn_root
    assert entry.manifest_url == repo.manifest_url
    assert entry.manifest_size == len(repo.manifest)
    assert entry.manifest_md5 == md5(repo.manifest).hex()
    assert entry.algorithm == 6


@pytest.mark.parametrize(
    "changes",
    [
        {"cos_repo_files": "not json"},
        {"cos_repo_files": '[{"cdn_root":"https://x","chunk_encrypt_flag":1,"manifest_files":[],'
         '"repository_id":50}]'},
        {"cos_access_info": "[]"},
    ],
)
def test_repositories_refuses_what_it_cannot_handle(changes):
    with pytest.raises(railrepo.RailError):
        railrepo.repositories(Repo(FILES).version_info(**changes))


def test_chunk_url_is_content_addressed():
    chunk = railrepo.Chunk(
        size=0x1F2, compressed_size=10, compressed_md5=b"\0" * 16,
        md5=bytes.fromhex("ab" * 16), method=0,
    )
    assert railrepo.chunk_url("https://cdn/x", chunk) == (
        "https://cdn/x/chunksv2/ab/" + "ab" * 16 + "1f2.wgc"
    )


# ---- chunks ----------------------------------------------------------------------------------
def test_decode_chunk_checks_both_hashes():
    raw = b"chunk-data" * 4
    chunk = railrepo.Chunk(len(raw), len(raw), md5(raw), md5(raw), 0)
    assert railrepo.decode_chunk(raw, chunk) == raw
    with pytest.raises(railrepo.RailError):
        railrepo.decode_chunk(raw[:-1] + b"X", chunk)
    lying = railrepo.Chunk(len(raw), len(raw), md5(raw), md5(b"other"), 0)
    with pytest.raises(railrepo.RailError):
        railrepo.decode_chunk(raw, lying)
    unknown = railrepo.Chunk(len(raw), len(raw), md5(raw), md5(raw), 9)
    with pytest.raises(railrepo.RailError):
        railrepo.decode_chunk(raw, unknown)


# ---- install ---------------------------------------------------------------------------------
def test_install_writes_the_whole_tree(tmp_path):
    repo = Repo(FILES)
    dest = tmp_path / "Launcher"
    result = _install(repo, dest)
    assert _tree(dest) == {
        "nikke_launcher.exe": FILES["nikke_launcher.exe"],
        "tpf_ui/skin/a.png": FILES["tpf_ui\\skin\\a.png"],
        "intl_service/INTLRuntime.dll": FILES["intl_service\\INTLRuntime.dll"],
        "empty.dat": b"",
    }
    assert (dest / "tpf_ui").is_dir()
    assert result.files == 4 and result.downloaded == 4 and result.kept == 0
    assert result.bytes == sum(len(v) for v in FILES.values() if v)
    assert not any(p.name.startswith(".nadare-") for p in dest.iterdir())


def test_a_second_install_keeps_verified_files_and_fetches_nothing(tmp_path):
    repo = Repo(FILES)
    dest = tmp_path / "Launcher"
    _install(repo, dest)
    fetched: list[str] = []

    def counting(url, size, md5_hex):
        fetched.append(url)
        return repo.fetch(url, size, md5_hex)

    result = railrepo.install(_repos(repo), dest, fetch=counting)
    assert result.kept == 4 and result.downloaded == 0
    assert fetched == [repo.manifest_url]


def test_a_damaged_file_is_replaced(tmp_path):
    repo = Repo(FILES)
    dest = tmp_path / "Launcher"
    _install(repo, dest)
    (dest / "nikke_launcher.exe").write_bytes(b"tampered")
    result = _install(repo, dest)
    assert result.downloaded == 1
    assert (dest / "nikke_launcher.exe").read_bytes() == FILES["nikke_launcher.exe"]


def test_files_outside_the_manifest_are_left_alone(tmp_path):
    repo = Repo(FILES)
    dest = tmp_path / "Launcher"
    dest.mkdir()
    (dest / "user-settings.ini").write_text("keep me")
    _install(repo, dest)
    assert (dest / "user-settings.ini").read_text() == "keep me"


@pytest.mark.parametrize(
    "bad",
    ["..\\evil.dll", "a\\..\\..\\evil.dll", "C:\\Windows\\evil.dll", "\\abs.dll", "a\\\\b",
     "nul\0.dll", "ok\\.\\x"],
)
def test_unsafe_paths_stop_the_install_before_anything_is_written(tmp_path, bad):
    repo = Repo({"good.txt": b"good", bad: b"evil"})
    dest = tmp_path / "Launcher"
    with pytest.raises(railrepo.RailError):
        _install(repo, dest)
    assert not (tmp_path / "evil.dll").exists()
    assert _tree(dest) == {} if dest.exists() else True


def test_paths_that_collide_on_windows_are_refused(tmp_path):
    repo = Repo({"Data\\a.txt": b"one", "data\\A.TXT": b"two"})
    with pytest.raises(railrepo.RailError):
        _install(repo, tmp_path / "Launcher")


def test_a_symlink_inside_the_destination_is_never_followed(tmp_path):
    repo = Repo({"tpf_ui\\a.txt": b"payload"})
    dest = tmp_path / "Launcher"
    outside = tmp_path / "outside"
    outside.mkdir()
    dest.mkdir()
    (dest / "tpf_ui").symlink_to(outside, target_is_directory=True)
    with pytest.raises(railrepo.RailError):
        _install(repo, dest)
    assert list(outside.iterdir()) == []


def test_a_symlinked_destination_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "Launcher").symlink_to(real, target_is_directory=True)
    with pytest.raises(railrepo.RailError):
        _install(Repo({"a.txt": b"x"}), tmp_path / "Launcher")


def test_a_corrupt_chunk_fails_and_leaves_no_partial_file(tmp_path):
    repo = Repo({"a.bin": b"0123456789abcdef"}, methods=(0,))
    url = next(u for u in repo.cdn if u.endswith(".wgc"))

    def corrupt(u, size, md5_hex):
        data = repo.cdn[u]
        return data[:-1] + bytes([data[-1] ^ 1]) if u == url else data

    dest = tmp_path / "Launcher"
    with pytest.raises(railrepo.RailError):
        railrepo.install(_repos(repo), dest, fetch=corrupt)
    assert not (dest / "a.bin").exists()
    assert _tree(dest) == {}


def test_cancel_stops_between_chunks_and_keeps_no_half_file(tmp_path):
    repo = Repo({"big.bin": os.urandom(64)}, chunk_size=8, methods=(0,))
    cancel = Cancel()
    calls = []

    def slow(u, size, md5_hex):
        calls.append(u)
        if len(calls) == 3:
            cancel.cancel()
        return repo.fetch(u, size, md5_hex)

    dest = tmp_path / "Launcher"
    with pytest.raises(Cancelled):
        railrepo.install(_repos(repo), dest, fetch=slow, cancel=cancel)
    assert not (dest / "big.bin").exists()
    assert len(calls) < 9


def test_progress_is_reported_as_a_fraction(tmp_path):
    repo = Repo(FILES)
    events = []
    _install(repo, tmp_path / "Launcher", emit=events.append)
    fractions = [e.progress for e in events if e.kind == "progress" and e.progress is not None]
    assert fractions and fractions[-1] == pytest.approx(1.0)
    assert fractions == sorted(fractions)


def test_files_are_verified_again_after_writing(tmp_path, monkeypatch):
    """A file that changes on disk between writing and the final check fails the install."""
    repo = Repo({"a.bin": b"abcdefgh" * 4})
    real_replace = os.replace

    def replace_then_tamper(src, dst):
        real_replace(src, dst)
        if str(dst).endswith("a.bin"):
            Path(dst).write_bytes(b"swapped!" * 4)

    monkeypatch.setattr(railrepo.os, "replace", replace_then_tamper)
    with pytest.raises(railrepo.RailError):
        _install(repo, tmp_path / "Launcher")


def test_the_md5_of_an_empty_file_is_accepted_without_chunks(tmp_path):
    repo = Repo({"empty.dat": b""})
    _install(repo, tmp_path / "Launcher")
    assert (tmp_path / "Launcher" / "empty.dat").read_bytes() == b""
    assert hashlib.md5(b"").digest() == md5(b"")  # noqa: S324


def test_a_path_inside_nadares_own_partial_folder_is_refused(tmp_path):
    repo = Repo({".NADARE-partial\\x.bin": b"sneaky"})
    with pytest.raises(railrepo.RailError):
        _install(repo, tmp_path / "Launcher")


# ---- #991 point 4: no staging left behind -------------------------------------------------------
def test_a_cancelled_install_leaves_no_empty_partial_folder(tmp_path):
    repo = Repo({"big.bin": os.urandom(64)}, chunk_size=8, methods=(0,))
    cancel = Cancel()
    calls = []

    def slow(u, size, md5_hex):
        calls.append(u)
        if len(calls) == 3:
            cancel.cancel()
        return repo.fetch(u, size, md5_hex)

    dest = tmp_path / "Launcher"
    with pytest.raises(Cancelled):
        railrepo.install(_repos(repo), dest, fetch=slow, cancel=cancel)
    assert not (dest / ".nadare-partial").exists()


def test_old_temporary_files_from_a_killed_run_are_swept(tmp_path):
    dest = tmp_path / "Launcher"
    partial = dest / ".nadare-partial"
    partial.mkdir(parents=True)
    stale = partial / "tmpabc123"
    stale.write_bytes(b"x" * 100)
    old = os.stat(stale).st_mtime - 2 * 3600
    os.utime(stale, (old, old))
    live = partial / "tmplive99"  # another process is writing this one right now
    live.write_bytes(b"y")
    keep = partial / "notes.txt"  # not one of ours: left alone
    keep.write_text("keep")
    os.utime(keep, (old, old))
    _install(Repo({"a.txt": b"hello"}), dest)
    assert not stale.exists()
    assert live.exists() and keep.exists()
    assert (dest / "a.txt").read_bytes() == b"hello"


def test_a_symlinked_destination_keeps_the_real_partial_folder(tmp_path):
    real = tmp_path / "real"
    (real / ".nadare-partial").mkdir(parents=True)
    dest = tmp_path / "Launcher"
    dest.symlink_to(real, target_is_directory=True)
    with pytest.raises(railrepo.RailError):
        _install(Repo({"a.txt": b"x"}), dest)
    assert (real / ".nadare-partial").is_dir()
