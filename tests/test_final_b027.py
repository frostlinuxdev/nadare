from pathlib import Path

from nadare.backends.nikke import hosts as h


def write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_save_cache_does_not_read_through_a_linked_parent_folder(tmp_path):
    src = tmp_path / "prefix-cache"
    write(src / "intl_auth.txt", b"session")
    secret = tmp_path / "home" / ".ssh"
    write(secret / "mmkv.default", b"PRIVATE KEY")
    (src / "mmkv").symlink_to(secret, target_is_directory=True)
    dst = tmp_path / "nadare-cache"

    try:
        h.save_cache(src, dst)
    except h.HostError:
        pass

    assert not (dst / "mmkv" / "mmkv.default").exists()
