# SPDX-License-Identifier: GPL-3.0-or-later
"""#1455 R4-7: one https-only check for every publisher URL (was 7 copies that disagreed:
gfl2 used ``startswith("https://")``, the rest ``urlsplit``; some crashed on a non-string)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nadare.core import directdl

SRC = Path(__file__).resolve().parents[1] / "src" / "nadare"


@pytest.mark.parametrize("url", ["https://cdn.example/a", "HTTPS://cdn.example/a"])
def test_accepts_https(url):
    assert directdl.require_https(url) == url


@pytest.mark.parametrize(
    "url", ["http://cdn.example/a", "ftp://x/a", "https:///nohost", "", None, 5, b"https://x"]
)
def test_refuses_anything_else(url):
    with pytest.raises(directdl.DirectError, match="https"):
        directdl.require_https(url, "patch file")


def test_the_message_names_what_was_checked():
    with pytest.raises(directdl.DirectError, match="manifest URL"):
        directdl.require_https("http://x/a", "manifest URL")


def test_no_source_keeps_its_own_copy():
    files = [*(SRC / "core" / "sources").glob("*.py"), SRC / "core" / "kuropatch.py"]
    own = []
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef) and node.name == "_https":
                own.append(path.name)
    assert not own, own
