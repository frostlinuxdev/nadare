# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.sources.nte against the recorded real manifest."""

from __future__ import annotations

import io
import zipfile
import zlib
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core.sources import nte

FIX = Path(__file__).parent / "fixtures" / "catalog"
RES = "https://ntecdn1.perfectworld.com/clientRes/PC_140"


def fx(name: str) -> bytes:
    return (FIX / name).read_bytes()


def container() -> bytes:
    with zipfile.ZipFile(io.BytesIO(fx("nte_ResList.bin.zip"))) as z:
        return z.read("ResList.bin")


class FakeFetch:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, headers=None):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected fetch: {url}")
        return self.routes[url]


def routes():
    return {
        nte.ENTRY_CONFIG_URL: fx("nte_config_entry.xml"),
        RES + "/Version/Windows/config.xml": fx("nte_config_branch.xml"),
        RES + "/Version/Windows/version/1.4.9/ResList.bin.zip": fx("nte_ResList.bin.zip"),
    }


def test_decode_container_reads_the_real_manifest():
    xml = nte.decode_container(container(), "3000001")
    assert xml.startswith('<?xml version="1.0" ?>\n<ResList version="1.4.9" tag="baseTag">')
    assert len(xml.encode()) == 131698  # the length stored in the header


def test_decode_container_wrong_app_id_fails_cleanly():
    with pytest.raises(dd.DirectError):
        nte.decode_container(container(), "3000002")


@pytest.mark.parametrize(
    "mangle",
    [lambda b: b"NotPatcher!!" + b[12:], lambda b: b[:100], lambda b: b[:-3], lambda b: b""],
)
def test_decode_container_rejects_damaged_input(mangle):
    with pytest.raises(dd.DirectError):
        nte.decode_container(mangle(container()), "3000001")


def test_resolve_builds_the_real_plan():
    f = FakeFetch(routes())
    plan = nte.resolve(f)
    assert plan.game_tag == "nte" and plan.version == "1.4.9" and plan.exe == nte.EXE
    assert len(plan.files) == 74 and len(plan.archives) == 6
    first = plan.files[0]
    assert first.path == "Client/WindowsNoEditor/Engine/Binaries/ThirdParty/CEF3/Win64/libcef.dll"
    assert first.size == 153114624 and first.md5 == "3a1cb9e5814c6a22261824396f79b55d"
    assert first.url == RES + "/Res/3/3a1cb9e5814c6a22261824396f79b55d.153114624"
    assert nte.EXE in {x.path for x in plan.files}
    assert sum(len(a.members) for a in plan.archives) == 443
    small = min(plan.archives, key=lambda a: a.source.size)
    assert small.unwrap == ("slices",) and small.source.size == 163387321
    assert small.source.md5 == "2e8df01b787e956f9b0efe9e60d35d5a"
    assert small.source.url == RES + "/Res/2/2e8df01b787e956f9b0efe9e60d35d5a.163387321"
    m = small.members[0]
    assert (m.path, m.offset, m.size, m.md5) == (
        "Client/WindowsNoEditor/HT/Content/PatchPaks/pakchunk5-Windows_0_P.sig",
        3249,
        532,
        "d492957b8026ec20fcd78ec465e29970",
    )
    for x in plan.files:
        dd.safe_relative_path(x.path)
    for a in plan.archives:
        dd.safe_relative_path(a.name)
        dd.safe_relative_path(a.source.path)
        for mem in a.members:
            dd.safe_relative_path(mem.path)
    assert f.calls[0] == nte.ENTRY_CONFIG_URL


def test_the_optional_tags_and_ignored_files_are_not_in_the_base_plan():
    plan = nte.resolve(FakeFetch(routes()))
    paths = {x.path for x in plan.files} | {m.path for a in plan.archives for m in a.members}
    assert not any("GameLauncher.config" in p or "patchersdk_serverlist" in p for p in paths)
    assert not any("TagPatchPaks/pakchunk104-Windows.pak" in p for p in paths)


def test_resolve_prefers_the_first_cdn_and_validates_the_config():
    r = routes()
    r[nte.ENTRY_CONFIG_URL] = b"<config>no resconfig here</config>"
    with pytest.raises(dd.DirectError):
        nte.resolve(FakeFetch(r))


XML = (
    '<?xml version="1.0" ?>\n<ResList version="9" tag="baseTag">\n'
    '<Res filename="{name}" filesize="10" md5="aa{p}"/>\n'
    '<Package><Pak md5="bb{p}" filesize="50"><Entry name="{ename}" start="1" offset="5" '
    'size="3" md5="cc{p}"/></Pak></Package></ResList>'
)
PAD = "0" * 30


def xml(name="a/b.bin", ename="c/d.bin"):
    return XML.format(name=name, ename=ename, p=PAD)


def test_plan_from_xml_minimal():
    plan = nte.plan_from_xml(xml(), RES, "9")
    (f,) = plan.files
    assert f.url == f"{RES}/Res/a/aa{PAD}.10" and f.size == 10
    (a,) = plan.archives
    assert a.source.url == f"{RES}/Res/b/bb{PAD}.50" and a.members[0].offset == 5


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/../../b", "a\\b", ""])
def test_plan_from_xml_rejects_hostile_names(bad):
    with pytest.raises(dd.DirectError):
        nte.plan_from_xml(xml(name=bad), RES, "9")
    with pytest.raises(dd.DirectError):
        nte.plan_from_xml(xml(ename=bad), RES, "9")


@pytest.mark.parametrize(
    "broken",
    [
        "not xml at all",
        '<ResList><Res filename="a" md5="x"/></ResList>',  # no filesize
        '<ResList><Res filename="a" filesize="zz" md5="aa"/></ResList>',
        '<ResList><Res filename="a" filesize="1" md5="not-hex!"/></ResList>',
    ],
)
def test_plan_from_xml_rejects_malformed_input(broken):
    with pytest.raises(dd.DirectError):
        nte.plan_from_xml(broken, RES, "9")


def test_plan_from_xml_refuses_non_https_roots():
    with pytest.raises(dd.DirectError):
        nte.plan_from_xml(xml(), "http://ntecdn1.perfectworld.com/clientRes/PC_140", "9")
