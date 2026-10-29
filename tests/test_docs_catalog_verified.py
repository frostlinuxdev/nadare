# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Keep the catalog's Verified list in sync with ``directkinds``."""

from __future__ import annotations

from pathlib import Path

from nadare.core.directkinds import DIRECT_WIZARD, LAUNCHER_WIZARD, VERIFIED_KINDS

REPO = Path(__file__).resolve().parents[1]


def test_every_verified_kind_is_named_in_the_catalog_docs() -> None:
    titles = {kind: title for kind, title, *_ in (*DIRECT_WIZARD, *LAUNCHER_WIZARD)}
    unknown = VERIFIED_KINDS - titles.keys()
    assert not unknown, f"VERIFIED_KINDS has kinds without a display title: {unknown}"

    catalog = (REPO / "docs" / "CATALOG.md").read_text(encoding="utf-8")
    missing = [
        f"{kind}: {titles[kind]}" for kind in sorted(VERIFIED_KINDS) if titles[kind] not in catalog
    ]
    assert not missing, "docs/CATALOG.md omits Verified games: " + ", ".join(missing)
