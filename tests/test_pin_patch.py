# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for scripts/pin_patch.py (offline patch pin generator)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

from nadare.core import patchspecs

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pin_patch.py"
KIBOTL = patchspecs.KIBOTL_EN


def _load_module():
    spec = importlib.util.spec_from_file_location("pin_patch", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_kibotl_zip(path: Path, contents: dict[str, bytes] | None = None) -> dict[str, bytes]:
    if contents is None:
        contents = {
            item.member: f"patched-{i}-{item.member}\n".encode()
            for i, item in enumerate(KIBOTL.files)
        }
    with zipfile.ZipFile(path, "w") as zf:
        for member, payload in contents.items():
            zf.writestr(member, payload)
    return contents


def test_pin_patch_writes_validated_index_doc_from_builtin_and_zip(tmp_path):
    mod = _load_module()
    zip_path = tmp_path / "en.zip"
    contents = _make_kibotl_zip(zip_path)
    index_dir = tmp_path / "index"

    rc = mod.main(["--id", "kibotl-en", "--zip", str(zip_path), "--index", str(index_dir)])
    assert rc == 0

    out = index_dir / "patches" / "kibotl-en.json"
    assert out.is_file()
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == 1
    assert doc["id"] == "kibotl-en"
    assert doc["kind"] == "azurpromilia-jp-yostar"
    assert doc["version"] == KIBOTL.version
    assert doc["url"] == KIBOTL.url
    assert doc["archive_sha256"] == _sha(zip_path.read_bytes())
    assert doc["archive_size"] == zip_path.stat().st_size
    assert len(doc["files"]) == len(KIBOTL.files)
    for item, entry in zip(KIBOTL.files, doc["files"], strict=True):
        assert entry["member"] == item.member
        assert entry["dest"] == item.dest
        assert entry["sha256"] == _sha(contents[item.member])
        assert entry["size"] == len(contents[item.member])
        assert entry["before_sha256"] == item.before_sha256
        assert entry["before_size"] == item.before_size
    assert patchspecs.spec_from_index(KIBOTL, doc) is not None


def test_pin_patch_uses_existing_index_doc_before_hashes_and_version_url_overrides(tmp_path):
    mod = _load_module()
    index_dir = tmp_path / "index"
    out = index_dir / "patches" / "kibotl-en.json"
    out.parent.mkdir(parents=True)

    custom_before_sha = "a" * 64
    existing = {
        "schema": 1,
        "id": "kibotl-en",
        "kind": "azurpromilia-jp-yostar",
        "version": "v2.0-9999",
        "url": "https://github.com/KiboTL/text/releases/download/v2.0-9999/en.zip",
        "archive_sha256": "b" * 64,
        "archive_size": 100,
        "files": [
            {
                "member": KIBOTL.files[0].member,
                "dest": KIBOTL.files[0].dest,
                "sha256": "c" * 64,
                "size": 50,
                "before_sha256": custom_before_sha,
                "before_size": 777,
            }
        ],
    }
    out.write_text(json.dumps(existing), encoding="utf-8")

    zip_path = tmp_path / "en2.zip"
    payload = b"second upload text"
    _make_kibotl_zip(zip_path, {KIBOTL.files[0].member: payload})

    rc = mod.main(
        [
            "--id",
            "kibotl-en",
            "--zip",
            str(zip_path),
            "--index",
            str(index_dir),
            "--version",
            "v2.1-9999",
            "--url",
            "https://github.com/KiboTL/text/releases/download/v2.1-9999/en.zip",
        ]
    )
    assert rc == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["version"] == "v2.1-9999"
    assert doc["url"] == "https://github.com/KiboTL/text/releases/download/v2.1-9999/en.zip"
    assert len(doc["files"]) == 1
    assert doc["files"][0]["sha256"] == _sha(payload)
    assert doc["files"][0]["size"] == len(payload)
    assert doc["files"][0]["before_sha256"] == custom_before_sha
    assert doc["files"][0]["before_size"] == 777


def test_pin_patch_missing_member_errors_and_writes_nothing(tmp_path, capsys):
    mod = _load_module()
    zip_path = tmp_path / "incomplete.zip"
    _make_kibotl_zip(zip_path, {KIBOTL.files[0].member: b"only one"})
    index_dir = tmp_path / "index"

    rc = mod.main(["--id", "kibotl-en", "--zip", str(zip_path), "--index", str(index_dir)])
    assert rc != 0
    assert not (index_dir / "patches" / "kibotl-en.json").exists()
    assert "missing" in capsys.readouterr().err.lower()


def test_pin_patch_before_from_recomputes_before_hashes(tmp_path):
    mod = _load_module()
    zip_path = tmp_path / "en.zip"
    _make_kibotl_zip(zip_path)
    game_dir = tmp_path / "game"
    pristine_bytes = {}
    for i, item in enumerate(KIBOTL.files):
        target = game_dir / item.dest
        target.parent.mkdir(parents=True, exist_ok=True)
        data = f"pristine-build-2-{i}\n".encode()
        target.write_bytes(data)
        pristine_bytes[item.dest] = data

    index_dir = tmp_path / "index"
    rc = mod.main(
        [
            "--id",
            "kibotl-en",
            "--zip",
            str(zip_path),
            "--index",
            str(index_dir),
            "--before-from",
            str(game_dir),
        ]
    )
    assert rc == 0
    doc = json.loads((index_dir / "patches" / "kibotl-en.json").read_text(encoding="utf-8"))
    for entry in doc["files"]:
        raw = pristine_bytes[entry["dest"]]
        assert entry["before_sha256"] == _sha(raw)
        assert entry["before_size"] == len(raw)


def test_pin_patch_refuses_invalid_url_and_writes_nothing(tmp_path, capsys):
    mod = _load_module()
    zip_path = tmp_path / "en.zip"
    _make_kibotl_zip(zip_path)
    index_dir = tmp_path / "index"

    rc = mod.main(
        [
            "--id",
            "kibotl-en",
            "--zip",
            str(zip_path),
            "--index",
            str(index_dir),
            "--url",
            "http://evil.invalid/en.zip",
        ]
    )
    assert rc != 0
    assert not (index_dir / "patches" / "kibotl-en.json").exists()
    assert capsys.readouterr().err.strip()
