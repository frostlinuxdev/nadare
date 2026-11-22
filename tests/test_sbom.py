# SPDX-License-Identifier: GPL-3.0-or-later
"""#1594 item 1: a CycloneDX SBOM built from what the release build already knows."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "sbom.py"

REQUIREMENTS = """\
# generated, do not edit
certifi==2026.10.23 \\
    --hash=sha256:a9895e6e2758b371e0545e321a3a8fd1fcfd9829ada8824a936dfb16aaaf36cd \\
    --hash=sha256:e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1
pycparser==3.11 ; implementation_name != 'PyPy' \\
    --hash=sha256:51d5a8ba2be0bbe440b99d2112604c95bbbc3c2748a64260186c541e1729cd80
"""


@pytest.fixture(scope="module")
def sbom():
    spec = importlib.util.spec_from_file_location("sbom_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def asset(tmp_path):
    path = tmp_path / "Nadare-0.1.0b25-x86_64.AppImage"
    path.write_bytes(b"not really an AppImage\n")
    return path


@pytest.fixture
def requirements(tmp_path):
    path = tmp_path / "requirements.txt"
    path.write_text(REQUIREMENTS, encoding="utf-8")
    return path


@pytest.fixture
def internal(tmp_path):
    folder = tmp_path / "_internal"
    (folder / "fallback-libs").mkdir(parents=True)
    (folder / "libffi.so.8").write_bytes(b"\x7fELF")
    (folder / "fallback-libs" / "libfreetype.so.6").write_bytes(b"\x7fELF")
    (folder / "libpython3.12.so.1.0").write_bytes(b"\x7fELF")
    return folder


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(sbom, asset, requirements, internal, **kwargs):
    return sbom.build_sbom(
        asset, requirements=requirements, internal=internal, root=ROOT, **kwargs
    )


def test_sbom_has_the_cyclonedx_1_6_shape(sbom, asset, requirements, internal):
    document = _build(sbom, asset, requirements, internal)
    assert document["bomFormat"] == "CycloneDX"
    assert document["specVersion"] == "1.6"
    assert document["version"] == 1
    assert document["serialNumber"].startswith("urn:uuid:")
    component = document["metadata"]["component"]
    assert component["name"] == asset.name
    assert component["hashes"] == [{"alg": "SHA-256", "content": _sha256(asset)}]
    assert document["components"]
    for entry in document["components"]:
        assert entry["type"] and entry["name"]
        assert entry["bom-ref"]


def test_the_asset_digest_matches_the_file(sbom, asset, requirements, internal):
    document = _build(sbom, asset, requirements, internal)
    digest = document["metadata"]["component"]["hashes"][0]["content"]
    assert digest == hashlib.sha256(asset.read_bytes()).hexdigest()


def test_a_requirement_with_hashes_becomes_a_component_with_that_hash(
    sbom, asset, requirements, internal
):
    document = _build(sbom, asset, requirements, internal)
    by_name = {entry["name"]: entry for entry in document["components"]}
    certifi = by_name["certifi"]
    assert certifi["version"] == "2026.10.23"
    assert certifi["purl"] == "pkg:pypi/certifi@2026.10.23"
    assert {
        (entry["alg"], entry["content"]) for entry in certifi["hashes"]
    } == {
        ("SHA-256", "a9895e6e2758b371e0545e321a3a8fd1fcfd9829ada8824a936dfb16aaaf36cd"),
        ("SHA-256", "e9b54de76e530bb6cf0ccd036787103acb58fd42ba930ea8bf948ddbaa2f61d1"),
    }


def test_a_requirement_marker_is_kept_as_a_property(sbom, asset, requirements, internal):
    document = _build(sbom, asset, requirements, internal)
    pycparser = next(entry for entry in document["components"] if entry["name"] == "pycparser")
    properties = {entry["name"]: entry["value"] for entry in pycparser["properties"]}
    assert properties["nadare:requirements-marker"] == "implementation_name != 'PyPy'"


def test_nadare_itself_and_the_bundled_libraries_are_components(
    sbom, asset, requirements, internal
):
    document = _build(sbom, asset, requirements, internal)
    by_name = {entry["name"]: entry for entry in document["components"]}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    nadare = by_name["nadare-launcher"]
    assert nadare["version"] == project["version"]
    assert nadare["licenses"] == [{"expression": project["license"]}]
    assert {"libffi.so.8", "libfreetype.so.6"} <= set(by_name)
    assert "libpython3.12.so.1.0" not in by_name


def test_a_bundled_library_carries_its_file_digest_and_path(
    sbom, asset, requirements, internal
):
    document = _build(sbom, asset, requirements, internal)
    by_name = {entry["name"]: entry for entry in document["components"]}
    library = by_name["libffi.so.8"]
    assert library["hashes"] == [
        {"alg": "SHA-256", "content": _sha256(internal / "libffi.so.8")}
    ]
    assert {"name": "nadare:bundled-path", "value": "libffi.so.8"} in library["properties"]


def test_the_timestamp_comes_from_source_date_epoch(
    sbom, asset, requirements, internal, monkeypatch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    document = _build(sbom, asset, requirements, internal)
    assert document["metadata"]["timestamp"] == "2023-11-14T22:13:20Z"
    monkeypatch.delenv("SOURCE_DATE_EPOCH")
    assert "timestamp" not in _build(sbom, asset, requirements, internal)["metadata"]


def test_two_runs_write_byte_identical_files(
    sbom, asset, requirements, internal, tmp_path, monkeypatch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    first = tmp_path / "first.cdx.json"
    second = tmp_path / "second.cdx.json"
    for output in (first, second):
        sbom.write_sbom(
            asset, output, requirements=requirements, internal=internal, root=ROOT
        )
    assert first.read_bytes() == second.read_bytes()


def test_the_cli_writes_the_requested_file(sbom, asset, requirements, internal, tmp_path):
    output = tmp_path / "asset.cdx.json"
    argv = [
        str(asset),
        "--out",
        str(output),
        "--requirements",
        str(requirements),
        "--internal",
        str(internal),
        "--root",
        str(ROOT),
    ]
    assert sbom.main(argv) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["metadata"]["component"]["hashes"][0]["content"] == _sha256(asset)
    assert output.read_text(encoding="utf-8").endswith("\n")


def test_building_the_sbom_uses_no_network(
    sbom, asset, requirements, internal, tmp_path, monkeypatch
):
    def refuse(*args, **kwargs):
        raise AssertionError("the SBOM must not open a network connection")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    output = tmp_path / "asset.cdx.json"
    argv = [
        str(asset),
        "--out",
        str(output),
        "--requirements",
        str(requirements),
        "--internal",
        str(internal),
        "--root",
        str(ROOT),
    ]
    assert sbom.main(argv) == 0


def test_the_packaging_doc_names_both_new_files():
    text = (ROOT / "docs" / "PACKAGING.md").read_text(encoding="utf-8")
    assert "packaging/sbom.py" in text
    assert "packaging/provenance.py" in text


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink"])
def test_output_cannot_overwrite_the_asset(
    sbom, asset, requirements, internal, tmp_path, alias
):
    output = asset
    if alias != "same":
        output = tmp_path / "output.cdx.json"
        if alias == "symlink":
            output.symlink_to(asset)
        else:
            output.hardlink_to(asset)
    original = asset.read_bytes()
    with pytest.raises(ValueError, match="asset"):
        sbom.write_sbom(
            asset, output, requirements=requirements, internal=internal, root=ROOT
        )
    assert asset.read_bytes() == original


def test_pypi_identifiers_use_canonical_names_and_encoded_versions(
    sbom, asset, requirements, internal, tmp_path
):
    requirements.write_text("Demo._-Package==1.0+local\n", encoding="utf-8")
    root = tmp_path / "source with spaces"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "Nadare._-Launcher"\nversion = "1.0+local"\n', encoding="utf-8"
    )
    document = sbom.build_sbom(
        asset, requirements=requirements, internal=internal, root=root
    )
    by_name = {entry["name"]: entry for entry in document["components"]}
    assert by_name["Demo._-Package"]["purl"] == "pkg:pypi/demo-package@1.0%2Blocal"
    assert by_name["Nadare._-Launcher"]["purl"] == "pkg:pypi/nadare-launcher@1.0%2Blocal"


def test_conditional_requirements_have_unique_refs_and_stable_order(
    sbom, asset, requirements, internal
):
    lines = [
        f'demo==1.0 ; sys_platform == "{platform}" --hash=sha256:{digit * 64}\n'
        for platform, digit in [("win32", "a"), ("linux", "b")]
    ]
    requirements.write_text("".join(lines + [lines[0]]), encoding="utf-8")
    first = _build(sbom, asset, requirements, internal)
    refs = [entry["bom-ref"] for entry in first["components"]]
    assert len(refs) == len(set(refs))
    demos = [entry for entry in first["components"] if entry["name"] == "demo"]
    assert len(demos) == 2
    assert {entry["hashes"][0]["content"] for entry in demos} == {"a" * 64, "b" * 64}
    requirements.write_text("".join(reversed(lines + [lines[0]])), encoding="utf-8")
    assert _build(sbom, asset, requirements, internal) == first


@pytest.mark.parametrize("epoch", ["not-an-integer", "9" * 100])
def test_invalid_epoch_is_a_clean_cli_error(
    sbom, asset, requirements, internal, tmp_path, monkeypatch, capsys, epoch
):
    monkeypatch.setenv("SOURCE_DATE_EPOCH", epoch)
    output = tmp_path / "bad-time.cdx.json"
    with pytest.raises(SystemExit) as error:
        sbom.main([
            str(asset), "--out", str(output), "--requirements", str(requirements),
            "--internal", str(internal),
        ])
    assert error.value.code == 1
    assert "SOURCE_DATE_EPOCH" in capsys.readouterr().err
    assert not output.exists()
