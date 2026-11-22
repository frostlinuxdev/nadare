# SPDX-License-Identifier: GPL-3.0-or-later
"""#1455 R4-8 / R4-10: one file hash, one ``real``/``inside`` (no private copies left).

Only one of the six ``_real`` copies survived a NUL byte in a hand-edited path (gamefinder's);
the others raised ValueError out of scans and Settings, the chaos FS-6 crash class."""

from __future__ import annotations

import ast
import hashlib
import importlib
from pathlib import Path

import pytest

from nadare.core import download, paths

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"

REAL = [
    "core.gamefinder",
    "core.gamefolders",
    "core.libraries",
    "core.location",
    "core.prefixguard",
    "core.profilefolders",
]
INSIDE = [
    "core.gamefolders",
    "core.flatpakfs",
    "core.gamesandbox",
    "core.libraries",
    "gui.add_game",
]
SHA = {
    "core.journal": "_sha256_file",
    "core.nte_launcher": "_sha256",
    "core.vcredist": "_sha256_file",
    "core.gamesandbox": "_sha256",
    "core.selfupdate": "_sha256_file",
}


def test_real_survives_a_nul_byte(tmp_path):
    import os

    assert paths.real("a\x00b") == Path(os.devnull)
    assert paths.real(tmp_path) == tmp_path.resolve()
    assert paths.real("~") == Path.home().resolve()


@pytest.mark.parametrize(
    "path,folder,expected",
    [
        ("/a/b/c", "/a/b", True),
        ("/a/b", "/a/b", True),
        ("/a/bc", "/a/b", False),
        ("/a", "/a/b", False),
    ],
)
def test_inside(path, folder, expected):
    assert paths.inside(Path(path), Path(folder)) is expected


@pytest.mark.parametrize("mod", sorted(set(REAL) | set(INSIDE)))
def test_no_private_copy(mod):
    tree = ast.parse((SRC / (mod.replace(".", "/") + ".py")).read_text(encoding="utf-8"))
    own = [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name in ("_real", "_inside")
    ]
    assert not own, f"{mod} keeps {own}"
    m = importlib.import_module(f"nadare.{mod}")
    if mod in REAL:
        assert m._real is paths.real
    if mod in INSIDE:
        assert m._inside is paths.inside


@pytest.mark.parametrize("mod,name", sorted(SHA.items()))
def test_sha256_copies_use_the_shared_hasher(mod, name, tmp_path, monkeypatch):
    f = tmp_path / "f.bin"
    f.write_bytes(b"abc")
    calls = []
    real = download.hash_file
    monkeypatch.setattr(
        download, "hash_file", lambda p, *a, **k: calls.append(p) or real(p, *a, **k)
    )
    m = importlib.import_module(f"nadare.{mod}")
    assert getattr(m, name)(f) == hashlib.sha256(b"abc").hexdigest()
    assert calls, f"{mod}.{name} hashes on its own"
