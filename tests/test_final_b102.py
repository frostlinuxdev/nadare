from nadare.core import sophon


def test_a_manifest_cannot_write_into_the_state_directory(tmp_path, monkeypatch):
    pkg = sophon.SophonPackage("game", "https://x/m", "https://x/c", "m1", 0)
    entry = sophon.FileEntry(".nadare/sophon-other.files", 0, "", ())
    monkeypatch.setattr(sophon, "_load_manifest", lambda *a, **k: [entry])
    try:
        sophon.sync_package(pkg, tmp_path)
    except sophon.SophonError:
        pass
    assert not (tmp_path / sophon.STATE_DIR / "sophon-other.files").exists()
