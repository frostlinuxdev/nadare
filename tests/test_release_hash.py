# SPDX-License-Identifier: GPL-3.0-or-later
"""#771: the release step that publishes the new AppImage's hash in the Nadare index."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "release_hash.py"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("release_hash_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _appimage(tmp_path: Path, body: bytes = b"appimage-bytes") -> tuple[Path, str]:
    path = tmp_path / "Nadare-0.1.0b20-x86_64.AppImage"
    path.write_bytes(body)
    return path, hashlib.sha256(body).hexdigest()


def test_the_first_release_creates_the_document(tool, tmp_path):
    image, digest = _appimage(tmp_path)
    index = tmp_path / "index"
    written = tool.add_release(index, "0.1.0b20", image)
    assert written == index / "app" / "releases.json"
    doc = json.loads(written.read_text(encoding="utf-8"))
    assert doc == {"schema": 1, "releases": {"0.1.0b20": {"x86_64": {"sha256": digest}}}}


def test_a_new_release_is_added_beside_the_old_ones(tool, tmp_path):
    image, digest = _appimage(tmp_path)
    index = tmp_path / "index"
    other = tmp_path / "other.AppImage"
    other.write_bytes(b"older")
    tool.add_release(index, "0.1.0b19", other)
    tool.add_release(index, "0.1.0b20", image)
    doc = json.loads((index / "app" / "releases.json").read_text(encoding="utf-8"))
    assert set(doc["releases"]) == {"0.1.0b19", "0.1.0b20"}
    assert doc["releases"]["0.1.0b20"]["x86_64"]["sha256"] == digest


def test_republishing_a_version_with_another_file_is_refused(tool, tmp_path):
    image, _ = _appimage(tmp_path)
    index = tmp_path / "index"
    tool.add_release(index, "0.1.0b20", image)
    different = tmp_path / "different.AppImage"
    different.write_bytes(b"not the same file")
    with pytest.raises(tool.ReleaseHashError, match="already"):
        tool.add_release(index, "0.1.0b20", different)


def test_republishing_the_same_file_is_fine(tool, tmp_path):
    image, _ = _appimage(tmp_path)
    index = tmp_path / "index"
    tool.add_release(index, "0.1.0b20", image)
    tool.add_release(index, "0.1.0b20", image)


@pytest.mark.parametrize("version", ["", "v0.1.0b20", "latest", "0.1.0b20/../x", "0.1"])
def test_a_bad_version_is_refused(tool, tmp_path, version):
    image, _ = _appimage(tmp_path)
    with pytest.raises(tool.ReleaseHashError):
        tool.add_release(tmp_path / "index", version, image)


def test_a_damaged_document_is_never_overwritten(tool, tmp_path):
    image, _ = _appimage(tmp_path)
    index = tmp_path / "index"
    (index / "app").mkdir(parents=True)
    doc = index / "app" / "releases.json"
    doc.write_text("{not json", encoding="utf-8")
    with pytest.raises(tool.ReleaseHashError, match="not valid"):
        tool.add_release(index, "0.1.0b20", image)
    assert doc.read_text(encoding="utf-8") == "{not json"


def test_only_the_newest_releases_are_kept(tool, tmp_path):
    index = tmp_path / "index"
    for minor in range(1, 41):
        image = tmp_path / f"{minor}.AppImage"
        image.write_bytes(str(minor).encode())
        tool.add_release(index, f"0.1.0b{minor}", image)
    doc = json.loads((index / "app" / "releases.json").read_text(encoding="utf-8"))
    assert len(doc["releases"]) == tool.KEEP
    assert "0.1.0b40" in doc["releases"] and "0.1.0b1" not in doc["releases"]


def test_the_written_document_is_accepted_by_the_app(tool, tmp_path):
    from nadare.core import selfupdate
    from nadare.core.updatecheck import UpdateInfo

    image, digest = _appimage(tmp_path)
    index = tmp_path / "index"
    written = tool.add_release(index, "0.1.0b20", image)
    doc = json.loads(written.read_text(encoding="utf-8"))
    info = UpdateInfo(
        "0.1.0b20", "https://github.com/frostlinuxdev/nadare/releases/tag/v0.1.0b20", "n"
    )
    expected = selfupdate.expected_sha256(
        info, fetch_sums=lambda url: "", fetch_index=lambda: doc
    )
    assert expected.sha256 == digest and expected.source == "index"


def test_a_missing_appimage_is_refused(tool, tmp_path):
    with pytest.raises(tool.ReleaseHashError, match="not a file"):
        tool.add_release(tmp_path / "index", "0.1.0b20", tmp_path / "missing.AppImage")
