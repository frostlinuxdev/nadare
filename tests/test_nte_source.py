# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.sources.nte against the recorded real manifest."""

from __future__ import annotations

import base64 as _b64
import io
import json as _json
import zipfile
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


# --- the branch chain: a branch config can point the client at the next branch ---
# Live 2026-11-21: publish_PC -> PC_140 (1.4.11), whose own ResConfig names PC_150 (1.5.5)
# with PreReleaseStatus "end". Stopping at PC_140 left 1.4 installs that the game server
# turns away after Start ("Update failed, please try again").


RES150 = "https://ntecdn1.perfectworld.com/clientRes/PC_150"
CDNS = ["https://ntecdn1.perfectworld.com/clientRes", "https://ntecdn2.perfectworld.com/clientRes"]


def _resconfig(branch: str) -> str:
    body = {"gameResUrl": CDNS, "branchName": branch, "appId": "3000001"}
    return _b64.b64encode(_json.dumps(body).encode()).decode()


def branch_cfg(
    version: str, next_branch: str | None = None, pre: tuple[str, str] | None = None
) -> bytes:
    parts = [f"<ResVersion>{version}</ResVersion>"]
    if pre:
        parts.append(
            f"<PreReleaseBranch>{pre[0]}</PreReleaseBranch><PreReleaseStatus>{pre[1]}</PreReleaseStatus>"
        )
    if next_branch:
        parts.append(f"<ResConfig>{_resconfig(next_branch)}</ResConfig>")
    return ('<?xml version="1.0" ?><config>' + "".join(parts) + "</config>").encode()


def chain_routes(pc140: bytes, pc150: bytes | None = None) -> dict:
    r = {
        nte.ENTRY_CONFIG_URL: fx("nte_config_entry.xml"),
        RES + "/Version/Windows/config.xml": pc140,
        RES + "/Version/Windows/version/1.4.11/ResList.bin.zip": fx("nte_ResList.bin.zip"),
    }
    if pc150 is not None:
        r[RES150 + "/Version/Windows/config.xml"] = pc150
        r[RES150 + "/Version/Windows/version/1.5.5/ResList.bin.zip"] = fx("nte_ResList.bin.zip")
    return r


def test_a_branch_that_names_the_next_branch_is_followed():
    f = FakeFetch(
        chain_routes(branch_cfg("1.4.11", "PC_150", ("PC_150", "end")), branch_cfg("1.5.5"))
    )
    plan = nte.resolve(f)
    assert plan.version == "1.5.5"
    assert all(x.url.startswith(RES150 + "/Res/") for x in plan.files)
    assert all(a.source.url.startswith(RES150 + "/Res/") for a in plan.archives)
    assert RES + "/Version/Windows/version/1.4.11/ResList.bin.zip" not in f.calls


def test_the_next_branch_is_followed_without_any_pre_release_fields():
    plan = nte.resolve(FakeFetch(chain_routes(branch_cfg("1.4.11", "PC_150"), branch_cfg("1.5.5"))))
    assert plan.version == "1.5.5"


@pytest.mark.parametrize("status", ["start", "predownload", ""])
def test_a_running_pre_release_is_not_installed_early(status):
    f = FakeFetch(
        chain_routes(branch_cfg("1.4.11", "PC_150", ("PC_150", status)), branch_cfg("1.5.5"))
    )
    plan = nte.resolve(f)
    assert plan.version == "1.4.11"
    assert RES150 + "/Version/Windows/config.xml" not in f.calls


def test_a_branch_naming_itself_stays_put():
    f = FakeFetch(chain_routes(branch_cfg("1.4.11", "PC_140")))
    assert nte.resolve(f).version == "1.4.11"


def test_a_branch_loop_fails_cleanly():
    with pytest.raises(dd.DirectError, match="loop"):
        nte.resolve(
            FakeFetch(chain_routes(branch_cfg("1.4.11", "PC_150"), branch_cfg("1.5.5", "PC_140")))
        )


def test_a_long_branch_chain_is_refused():
    r = {nte.ENTRY_CONFIG_URL: fx("nte_config_entry.xml")}
    names = [f"PC_{n}" for n in range(140, 160)]
    for here, nxt in zip(names[:-1], names[1:], strict=True):
        r[f"https://ntecdn1.perfectworld.com/clientRes/{here}/Version/Windows/config.xml"] = (
            branch_cfg("1.4.11", nxt)
        )
    with pytest.raises(dd.DirectError, match="too many"):
        nte.resolve(FakeFetch(r))


def test_a_damaged_branch_resconfig_fails_cleanly():
    bad = (
        b'<?xml version="1.0" ?><config>'
        b"<ResVersion>1.4.11</ResVersion><ResConfig>%%%</ResConfig></config>"
    )
    with pytest.raises(dd.DirectError):
        nte.resolve(FakeFetch(chain_routes(bad)))


@pytest.mark.parametrize(
    ("legacy", "no_paks"),
    [(False, False), (True, False), (False, True)],
    ids=["slotted", "record-before-the-fix", "next-version-all-loose"],
)
def test_an_update_removes_members_of_a_pak_it_replaced(tmp_path, legacy, no_paks):
    """#1824: each changed Pak is a new object (pak-<md5>); its dropped members must go.

    ``legacy``: the first version was installed before Paks had a slot (its records have none).
    ``no_paks``: the next version ships every file loose, so its plan has no archive at all.
    """
    import dataclasses
    import hashlib

    from _httpserver import Server

    def md5(b):
        return hashlib.md5(b).hexdigest()

    def pak(entries):
        blob = b"HDR!" + b"".join(entries.values())
        rows, offset = [], 4
        for name, data in entries.items():
            rows.append(
                f'<Entry name="{name}" start="0" offset="{offset}" size="{len(data)}" '
                f'md5="{md5(data)}"/>'
            )
            offset += len(data)
        return blob, f'<Pak md5="{md5(blob)}" filesize="{len(blob)}">{"".join(rows)}</Pak>'

    exe = b"MZ"
    unchanged = {"b/same.pak": b"same"}
    dest = tmp_path / "game"
    later = {"a/keep.pak": b"keep", "a/new.pak": b"new!"}
    for n, changing in enumerate(({"a/keep.pak": b"keep", "a/old.pak": b"old!"}, later)):
        blobs = {}
        xml = ['<?xml version="1.0" ?>\n<ResList version="9" tag="baseTag">',
               f'<Res filename="{nte.EXE}" filesize="2" md5="{md5(exe)}"/>', "<Package>"]
        for entries in (unchanged, changing) if n == 0 or not no_paks else ():
            blob, row = pak(entries)
            blobs[md5(blob)] = blob
            xml.append(row)
        xml.append("</Package>")
        if n and no_paks:  # every file loose: the plan has no archive (and no slot) at all
            for name, data in {**unchanged, **later}.items():
                xml.append(f'<Res filename="{name}" filesize="{len(data)}" md5="{md5(data)}"/>')
                blobs[md5(data)] = data
        xml.append("</ResList>")
        plan = nte.plan_from_xml("\n".join(xml), RES, str(9 + n))
        s = Server({**{f"/{k}": v for k, v in blobs.items()}, f"/{md5(exe)}": exe})
        try:
            local = dataclasses.replace(
                plan,
                files=tuple(dataclasses.replace(f, url=f"{s.url}/{f.md5}") for f in plan.files),
                archives=tuple(
                    dataclasses.replace(
                        a,
                        source=dataclasses.replace(a.source, url=f"{s.url}/{a.source.md5}"),
                        slot="" if legacy and n == 0 else a.slot,
                    )
                    for a in plan.archives
                ),
            )
            dd.sync(local, dest)
        finally:
            s.close()
    assert (dest / "a/new.pak").read_bytes() == b"new!"
    assert (dest / "a/keep.pak").read_bytes() == b"keep"
    assert (dest / "b/same.pak").read_bytes() == b"same"
    assert not (dest / "a/old.pak").exists()
