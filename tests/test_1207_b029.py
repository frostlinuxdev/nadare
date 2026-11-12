from pathlib import Path

from nadare.backends.nikke import hosts as h


def write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_restore_cache_does_not_copy_a_linked_cached_file(tmp_path):
    secret = write(tmp_path / "home" / "private", b"PRIVATE KEY")
    cache = tmp_path / "cache"
    write(cache / "mmkv" / "mmkv.default", b"device")
    (cache / "intl_auth.txt").symlink_to(secret)
    dst = tmp_path / "dst"

    try:
        h.restore_cache(cache, dst)
    except h.HostError:
        pass

    assert not (dst / "intl_auth.txt").exists()
