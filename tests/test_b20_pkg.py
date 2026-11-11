# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Regression guards for the b20 packaging follow-ups (#38, #141, #181, #257,
#588, #599). Static checks on the recipes, plus one close reading of each unit."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# #38 - the optional units stay system units that read a root-only env file
# ---------------------------------------------------------------------------
class TestMaintainerUnits:
    def test_the_pin_unit_still_reads_its_root_only_environment_file(self):
        # A `systemctl --user` unit cannot read a root-owned env file (mode 600); it has to
        # run as a system unit with User= set.
        service = _text("packaging/systemd/nadare-pin-publisher.service")
        assert "EnvironmentFile=-/etc/nadare-pin-publisher.env" in service
        assert "WantedBy=default.target" not in service

    def test_the_deb_does_not_ship_the_maintainer_units(self):
        assert "systemd" not in _text("scripts/build_deb.sh")


# ---------------------------------------------------------------------------
# #141 — the DXVK present-timing workaround is automatic now
# ---------------------------------------------------------------------------
def test_troubleshooting_does_not_tell_users_to_set_dxvk_config_by_hand():
    doc = _text("docs/TROUBLESHOOTING.md")
    row = next(line for line in doc.splitlines() if "DXVK 3.1.1" in line)
    assert "automatically" in row  # the catalog games need no profile edit
    assert "sets `DXVK_CONFIG`" in row or "sets DXVK_CONFIG" in row


# ---------------------------------------------------------------------------
# #181 — zsync metadata next to the AppImage when zsyncmake is available
# ---------------------------------------------------------------------------
def test_appimage_build_writes_zsync_metadata_when_the_tool_exists():
    script = _text("scripts/build_appimage.sh")
    assert "zsyncmake" in script
    assert ".zsync" in script


# ---------------------------------------------------------------------------
# #257 — CI rebuilds the CZN stub and compares it with the committed binary
# ---------------------------------------------------------------------------
class TestStubIsRebuiltInCI:
    def test_workflow_builds_the_stub_and_compares_it(self):
        for rel in (".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"):
            text = _text(rel)
            assert "build_stub.sh --container" in text, rel
            assert "czn_pcsdk_stub.exe" in text, rel
            assert "cmp " in text or "sha256sum" in text, rel


# ---------------------------------------------------------------------------
# #588 — a scheduled dependency audit
# ---------------------------------------------------------------------------
class TestDependencyAudit:
    def test_workflow_runs_a_scheduled_audit(self):
        for rel in (".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"):
            text = _text(rel)
            assert "schedule:" in text, rel
            assert "pip-audit" in text, rel


# ---------------------------------------------------------------------------
# #599 — packaging artefacts match what they ship
# ---------------------------------------------------------------------------
class TestPackagingArtefacts:
    def test_deb_fetches_the_bundled_7zip_before_pyinstaller(self):
        deb = _text("scripts/build_deb.sh")
        assert "bash scripts/fetch_7zip.sh" in deb
        assert deb.index("fetch_7zip.sh") < deb.index("-m PyInstaller")

    def test_deb_derives_the_glibc_floor_from_the_bundle(self):
        deb = _text("scripts/build_deb.sh")
        assert "readelf" in deb
        assert re.search(r"libc6 \(>= \$?\{?glibc", deb)

    def test_metainfo_has_the_appstream_identity_fields(self):
        meta = _text("packaging/flatpak/io.github.frostlinuxdev.Nadare.metainfo.xml")
        assert '<url type="homepage">' in meta
        assert "<developer" in meta

    def test_appimage_stages_the_metainfo(self):
        script = _text("scripts/build_appimage.sh")
        assert "metainfo" in script
        assert "usr/share/metainfo" in script


# ---------------------------------------------------------------------------
# #185 / #190 — one packaging doc, with the update-owner table
# ---------------------------------------------------------------------------
class TestPackagingDoc:
    def test_doc_exists_with_the_channel_table(self):
        doc = _text("docs/PACKAGING.md")
        for channel in ("AppImage", "Flatpak", "AUR", ".deb"):
            assert channel in doc
        assert "NADARE_MANAGED_UPDATES" in doc
        assert "docs/PACKAGING.md" not in _text("docs/PACKAGING.md")  # no self-link



def test_aur_recipe_version_is_consistent_across_pkgbuild_and_srcinfo():
    """The digest cannot be regenerated offline; at least keep the two files in step."""
    pkgbuild = _text("packaging/aur/nadare-bin/PKGBUILD")
    match = re.search(r"^pkgver=(.+)$", pkgbuild, re.M)
    assert match is not None
    assert f"pkgver = {match.group(1)}" in _text("packaging/aur/nadare-bin/.SRCINFO")
    assert 'provides=("${_pkgname}=${pkgver}")' in pkgbuild


# ---------------------------------------------------------------------------
# review wave 3: findings fixed in b20
# ---------------------------------------------------------------------------
class TestReviewFixes:
    def test_the_deb_launcher_marks_updates_as_package_managed(self):
        deb = _text("scripts/build_deb.sh")
        assert "NADARE_MANAGED_UPDATES=1" in deb
        assert 'ln -sf /opt/nadare/nadare "$pkg/usr/bin/nadare"' not in deb

    def test_the_glibc_probe_cannot_abort_the_build(self):
        deb = _text("scripts/build_deb.sh")
        start = deb.index("glibc_found=")
        block = deb[start : deb.index("\nfi\n", start)]
        assert "|| true" in block

    def test_both_scheduled_jobs_can_be_started_by_hand(self):
        for rel in (".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"):
            text = _text(rel)
            audit = text[text.index("\n  audit:") :]
            assert "workflow_dispatch" in audit.split("runs-on")[0], rel

    def test_a_scheduled_run_does_not_share_a_cancel_group_with_pushes(self):
        for rel in (".github/workflows/ci.yml", ".forgejo/workflows/ci.yml"):
            text = _text(rel)
            group = text[text.index("concurrency:") : text.index("cancel-in-progress")]
            assert "github.event_name" in group, rel

    def test_the_licence_claim_matches_what_the_bundle_ships(self):
        doc = _text("docs/PACKAGING.md")
        assert "carries only Nadare's `LICENSE`" not in doc
        assert "7-Zip" in doc

    def test_the_dxvk_row_names_where_the_default_applies(self):
        row = next(
            line for line in _text("docs/TROUBLESHOOTING.md").splitlines() if "DXVK 3.1.1" in line
        )
        assert "every Wine/Proton profile" not in row
        assert "extra_env" in row  # a custom Wine/Proton game profile still sets it by hand

    def test_the_packaging_doc_does_not_point_at_a_privacy_section_that_is_not_there(self):
        assert "described in [PRIVACY.md]" not in _text("docs/PACKAGING.md")
