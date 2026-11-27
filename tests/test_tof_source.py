# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic Tower of Fantasy manifests and local object downloads."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core import directslices
from nadare.core.sources import _aes, nte, tof

# The live 6.4.3 feed's game exe (a Res row).
TOF_EXE = "Client/WindowsNoEditor/Hotta/Binaries/Win64/QRSL.exe"

FIX = Path(__file__).parent / "fixtures" / "catalog"
ROOT = "https://htkrydpatch1.wmupd.com/clientRes/OBPC_2_5"
CONFIG = ROOT + "/Version/Windows/config.xml"
XML = (FIX / "tof_reslist.xml").read_text()
CFG = (FIX / "tof_config.xml").read_bytes()


def encrypted(xml):
    """Test-only forward AES, using the shipped AES tables and key expansion."""
    plain = zlib.compress(xml.encode())
    padding = 16 - len(plain) % 16
    plain += bytes([padding]) * padding
    keys = _aes._expand_key((str(1000129) + "@Patcher").ljust(16, "0").encode())
    prev = b"PatcherSDK".ljust(16, b"0")
    output = bytearray()
    for offset in range(0, len(plain), 16):
        state = [
            a ^ b ^ k for a, b, k in zip(plain[offset : offset + 16], prev, keys[0], strict=True)
        ]
        for rnd in range(1, 11):
            state = [_aes._SBOX[b] for b in state]
            state = [state[(i % 4) + 4 * ((i // 4 + i % 4) % 4)] for i in range(16)]
            if rnd < 10:
                mixed = []
                for c in range(0, 16, 4):
                    a, b, c1, d = state[c : c + 4]
                    mixed.extend(
                        (
                            _aes._gmul(a, 2) ^ _aes._gmul(b, 3) ^ c1 ^ d,
                            a ^ _aes._gmul(b, 2) ^ _aes._gmul(c1, 3) ^ d,
                            a ^ b ^ _aes._gmul(c1, 2) ^ _aes._gmul(d, 3),
                            _aes._gmul(a, 3) ^ b ^ c1 ^ _aes._gmul(d, 2),
                        )
                    )
                state = mixed
            state = [a ^ b for a, b in zip(state, keys[rnd], strict=True)]
        prev = bytes(state)
        output.extend(prev)
    return nte.MAGIC + struct.pack("<I", len(xml.encode())) + output


def resolve(xml=XML, config=CFG, container=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("ResList.bin", encrypted(xml) if container is None else container)
        archive.writestr("lastdiff.bin", b"unused")
    version = (
        ET.fromstring(config).findtext("ResVersion") if config.startswith(b"<config>") else "?"
    )
    routes = {
        CONFIG: config,
        ROOT + f"/Version/Windows/version/{version}/ResList.bin.zip": buf.getvalue(),
    }
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        return routes[url]

    return tof.resolve(fetch), calls


def test_encrypted_feed_builds_exact_plan():
    plan, calls = resolve()
    assert (plan.game_tag, plan.version, plan.exe) == ("tof", "6.4.3", TOF_EXE)
    assert calls == [
        (CONFIG, None),
        (ROOT + "/Version/Windows/version/6.4.3/ResList.bin.zip", None),
    ]
    assert len(plan.files) == len(plan.archives) == 1
    file = plan.files[0]
    assert (file.path, file.size, file.md5) == (TOF_EXE, 2, hashlib.md5(b"MZ").hexdigest())
    arc = plan.archives[0]
    assert arc.unwrap == ("slices",) and arc.source.size == 12
    assert arc.source.md5 == hashlib.md5(b"PADhelloxbye").hexdigest()
    assert [(m.path, m.offset, m.size, m.md5) for m in arc.members] == [
        ("data/b.bin", 9, 3, hashlib.md5(b"bye").hexdigest()),
        ("data/a.bin", 3, 5, hashlib.md5(b"hello").hexdigest()),
    ]
    assert (
        sum(f.size for f in plan.files) + sum(m.size for a in plan.archives for m in a.members)
        == 10
    )
    for obj in (file, arc.source):
        assert obj.url == f"{ROOT}/Res/{obj.md5[0]}/{obj.md5}.{obj.size}"


@pytest.mark.parametrize("value", [None, 3, {}, b"xml", "bad xml", "<!DOCTYPE x><ResList/>"])
def test_rejects_wrong_manifest_types(value):
    with pytest.raises(dd.DirectError):
        tof.plan_from_xml(value, "6.4.3", 10)


@pytest.mark.parametrize(
    "tag,attr,bad",
    [
        (tag, attr, bad)
        for tag, attr in (("Res", "filename"), ("Entry", "name"))
        for bad in (None, "", "../escape", "/absolute", "a/../../x", "a\\b", "C:/escape")
    ]
    + [
        (tag, attr, bad)
        for tag, attr in (("Res", "md5"), ("Pak", "md5"), ("Entry", "md5"))
        for bad in (None, "wrong", "g" * 32)
    ]
    + [
        (tag, attr, bad)
        for tag, attr in (
            ("Res", "filesize"),
            ("Pak", "filesize"),
            ("Entry", "size"),
            ("Entry", "offset"),
        )
        for bad in (None, "-1", "no", "1.5")
    ]
    + [("Entry", "offset", "100")],
)
def test_rejects_bad_manifest_fields(tag, attr, bad):
    root = ET.fromstring(XML)
    node = root.find(".//" + tag)
    if bad is None:
        del node.attrib[attr]
    else:
        node.set(attr, bad)
    with pytest.raises(dd.DirectError):
        tof.plan_from_xml(ET.tostring(root).decode(), "6.4.3", 10)


@pytest.mark.parametrize(
    "config",
    [
        b"bad",
        b"<config/>",
        CFG.replace(b"6.4.3", b"../bad"),
        CFG.replace(b"<ResVersion>6.4.3</ResVersion>", b""),
        CFG.replace(b"<ResSize>10</ResSize>", b""),
        CFG.replace(b">10<", b">-1<"),
        CFG.replace(b">10<", b">oops<"),
    ],
)
def test_rejects_bad_config_or_size_total(config):
    with pytest.raises(dd.DirectError):
        resolve(config=config)


def test_accepts_a_published_res_size_that_differs_from_the_plan_sum():
    """R1999 showed publisher totals are approximate; the plan total is the truth."""
    plan = tof.plan_from_xml(XML, "6.4.3", 11)
    assert plan.extra["installed_size"] == 10
    live, _ = resolve(config=CFG.replace(b">10<", b">11<"))
    assert live.extra["installed_size"] == 10


def test_accepts_a_res_size_that_counts_ignored_rows():
    """The publisher's ResSize includes files the client writes itself; the plan does not."""
    root = ET.fromstring(XML)
    ET.SubElement(
        root,
        "Res",
        {
            "filename": "ignored.bin",
            "filesize": "4",
            "md5": hashlib.md5(b"skip").hexdigest(),
        },
    )
    ET.SubElement(ET.SubElement(root, "Ignored"), "Item", {"name": "ignored.bin"})
    plan = tof.plan_from_xml(ET.tostring(root, encoding="unicode"), "6.4.3", 14)
    assert plan.extra["installed_size"] == 10
    assert all(item.path != "ignored.bin" for item in plan.files)


def test_rejects_case_insensitive_duplicate_install_paths():
    root = ET.fromstring(XML)
    ET.SubElement(
        root,
        "Res",
        {"filename": TOF_EXE.upper(), "filesize": "2", "md5": hashlib.md5(b"MZ").hexdigest()},
    )
    with pytest.raises(dd.DirectError):
        tof.plan_from_xml(ET.tostring(root, encoding="unicode"), "6.4.3", 12)


def test_rejects_duplicate_pak_names():
    root = ET.fromstring(XML)
    pak = root.find("Package/Pak")
    assert pak is not None
    second = ET.SubElement(
        root.find("Package"),  # type: ignore[arg-type]
        "Pak",
        {"filesize": "5", "md5": pak.get("md5")},  # type: ignore[dict-item]
    )
    ET.SubElement(
        second,
        "Entry",
        {
            "name": "data/c.bin",
            "offset": "0",
            "size": "5",
            "md5": hashlib.md5(b"hello").hexdigest(),
        },
    )
    with pytest.raises(dd.DirectError):
        tof.plan_from_xml(ET.tostring(root, encoding="unicode"), "6.4.3", 15)


def test_rejects_a_manifest_that_does_not_install_the_game_exe():
    with pytest.raises(dd.DirectError):
        tof.plan_from_xml(XML.replace(TOF_EXE, "Client/Other.exe"), "6.4.3", 10)


def test_rejects_an_empty_manifest():
    with pytest.raises(dd.DirectError):
        tof.plan_from_xml('<ResList version="6.4.3"/>', "6.4.3", 0)


@pytest.mark.parametrize("body", [b"", b"NotPatcher" * 4, encrypted(XML)[:-1]])
def test_rejects_damaged_container(body):
    with pytest.raises(dd.DirectError):
        resolve(container=body)


def test_rejects_missing_zip_member():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("lastdiff.bin", b"unused")
    with pytest.raises(dd.DirectError):
        tof.resolve(lambda url, headers: CFG if url == CONFIG else buf.getvalue())


class MemoryTransport:
    """Only replace byte transfer; directdl verifies and installs the real payloads."""

    url = "http://127.0.0.1:9999"

    def __init__(self, objects, monkeypatch):
        self.objects = objects
        self.requests = []

        def download(url, dest, **kwargs):
            path = url.removeprefix(self.url)
            self.requests.append(path)
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(self.objects[path])

        monkeypatch.setattr(dd, "download", download)

    def close(self):
        pass


def local_plan(plan, server):
    def remote(obj):
        return dataclasses.replace(obj, url=obj.url.replace(ROOT, server.url))

    return dataclasses.replace(
        plan,
        files=tuple(remote(f) for f in plan.files),
        archives=tuple(dataclasses.replace(a, source=remote(a.source)) for a in plan.archives),
    )


def install_plan(tmp_path, monkeypatch):
    """Install the synthetic 6.4.3 plan locally and return it with its transport."""
    plan, _ = resolve()
    objects = {
        plan.files[0].url.removeprefix(ROOT): b"MZ",
        plan.archives[0].source.url.removeprefix(ROOT): b"PADhelloxbye",
    }
    server = MemoryTransport(objects, monkeypatch)
    plan = local_plan(plan, server)
    assert dd.sync(plan, tmp_path) == {"downloaded": 2, "skipped": 0}
    return plan, server


def version_only_update(server, version="6.4.4"):
    """The same manifest under a new ResVersion: only the Pak's version moves."""
    update, _ = resolve(XML, CFG.replace(b"6.4.3", version.encode()))
    return local_plan(update, server)


def test_a_reused_pak_records_the_new_version(tmp_path, monkeypatch):
    _plan, server = install_plan(tmp_path, monkeypatch)
    changed = XML.replace(hashlib.md5(b"MZ").hexdigest(), hashlib.md5(b"NZ").hexdigest())
    update, _ = resolve(changed, CFG.replace(b"6.4.3", b"6.4.4"))
    update = local_plan(update, server)
    new_file = update.files[0].url.removeprefix(server.url)
    server.objects[new_file] = b"NZ"
    count = len(server.requests)
    assert dd.sync(update, tmp_path) == {"downloaded": 1, "skipped": 1}
    assert server.requests[count:] == [new_file]  # the unchanged Pak was not fetched
    record = tmp_path / dd.STATE_DIR / f"{update.archives[0].name}.done"
    assert json.loads(record.read_text())["version"] == "6.4.4"


def test_plan_missing_without_verification_does_not_hash_a_reused_pak(tmp_path, monkeypatch):
    _plan, server = install_plan(tmp_path, monkeypatch)
    update = version_only_update(server)
    hashed = []
    real = dd._digest_pass

    def spy(*args, **kwargs):
        hashed.append(Path(args[0]).name)
        return real(*args, **kwargs)

    monkeypatch.setattr(dd, "_digest_pass", spy)
    assert dd.plan_missing(update, tmp_path, verify=False) == ([], [])
    assert hashed == []


def test_plan_missing_verification_hashes_reused_pak_members_once(tmp_path, monkeypatch):
    _plan, server = install_plan(tmp_path, monkeypatch)
    update = version_only_update(server)
    digests, crcs = [], []
    real_digest, real_crc = dd._digest_pass, dd._crc32_file

    def spy_digest(*args, **kwargs):
        digests.append(Path(args[0]).name)
        return real_digest(*args, **kwargs)

    def spy_crc(path):
        crcs.append(Path(path).name)
        return real_crc(path)

    monkeypatch.setattr(dd, "_digest_pass", spy_digest)
    monkeypatch.setattr(dd, "_crc32_file", spy_crc)
    assert dd.plan_missing(update, tmp_path) == ([], [])
    assert sorted(n for n in digests if n in ("a.bin", "b.bin")) == ["a.bin", "b.bin"]
    assert crcs == []


def test_plan_missing_checks_the_record_before_hashing_members(tmp_path, monkeypatch):
    _plan, server = install_plan(tmp_path, monkeypatch)
    update = version_only_update(server)
    record_path = tmp_path / dd.STATE_DIR / f"{update.archives[0].name}.done"
    record = json.loads(record_path.read_text())
    record["member_sizes"]["data/a.bin"] += 1
    record_path.write_text(json.dumps(record))
    hashed = []
    real = dd._digest_pass

    def spy(*args, **kwargs):
        hashed.append(Path(args[0]).name)
        return real(*args, **kwargs)

    monkeypatch.setattr(dd, "_digest_pass", spy)
    files, archives = dd.plan_missing(update, tmp_path)
    assert files == [] and archives == [update.archives[0]]
    assert not [name for name in hashed if name in ("a.bin", "b.bin")]


def test_archive_reuse_uses_the_directslices_module(tmp_path, monkeypatch):
    _plan, server = install_plan(tmp_path, monkeypatch)
    update = version_only_update(server)
    calls = []

    def fake(archive, record, dest, **kwargs):
        calls.append((archive.name, kwargs))
        return False

    monkeypatch.setattr(directslices, "matches", fake)
    files, archives = dd.plan_missing(update, tmp_path)
    assert files == [] and archives == [update.archives[0]]
    assert calls and calls[0] == (update.archives[0].name, {"hashes": True})


def slice_archive(member_path="data/a.bin"):
    source = dd.RemoteFile(
        path="pak-00000000000000000000000000000000",
        url="https://example.test/pak",
        size=5,
        md5="a" * 32,
    )
    member = dd.Member(
        path=member_path, offset=0, size=5, md5=hashlib.md5(b"hello").hexdigest()
    )
    return dd.Archive(name="pak", source=source, unwrap=("slices",), members=(member,))


@pytest.mark.parametrize("record_path", ["data/a.bin", "./data/a.bin", "data//a.bin"])
def test_directslices_matches_normalises_both_sides(tmp_path, record_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "a.bin").write_bytes(b"hello")
    archive = slice_archive("./data/a.bin")
    assert directslices.matches(archive, {"members": [record_path]}, tmp_path)


@pytest.mark.parametrize("members", [["../escape"], ["/absolute"], ["a\\b"], []])
def test_directslices_matches_rejects_bad_or_empty_records(tmp_path, members):
    archive = slice_archive("../escape")
    assert directslices.matches(archive, {"members": members}, tmp_path) is False


def test_directslices_matches_rejects_an_empty_member_list(tmp_path):
    archive = dataclasses.replace(slice_archive(), members=())
    assert directslices.matches(archive, {"members": []}, tmp_path) is False


def test_install_and_new_version_fetch_only_changed_objects(tmp_path, monkeypatch):
    plan, _ = resolve()
    changed = XML.replace(hashlib.md5(b"MZ").hexdigest(), hashlib.md5(b"NZ").hexdigest())
    update, calls = resolve(changed, CFG.replace(b"6.4.3", b"6.4.4"))
    assert calls[-1][0].endswith("/6.4.4/ResList.bin.zip")
    objects = {
        plan.files[0].url.removeprefix(ROOT): b"MZ",
        update.files[0].url.removeprefix(ROOT): b"NZ",
        plan.archives[0].source.url.removeprefix(ROOT): b"PADhelloxbye",
    }
    server = MemoryTransport(objects, monkeypatch)
    try:
        assert dd.sync(local_plan(plan, server), tmp_path) == {"downloaded": 2, "skipped": 0}
        assert (tmp_path / TOF_EXE).read_bytes() == b"MZ"
        assert (tmp_path / "data/a.bin").read_bytes() == b"hello"
        assert (tmp_path / "data/b.bin").read_bytes() == b"bye"
        count = len(server.requests)
        assert dd.sync(local_plan(update, server), tmp_path) == {"downloaded": 1, "skipped": 1}
        assert server.requests[count:] == [update.files[0].url.removeprefix(ROOT)]
        assert (tmp_path / TOF_EXE).read_bytes() == b"NZ"
        # A changed Pak is fetched whole and all its new members are installed.
        xml2 = changed.replace(
            hashlib.md5(b"PADhelloxbye").hexdigest(), hashlib.md5(b"PADHELLOxbye").hexdigest()
        )
        xml2 = xml2.replace(hashlib.md5(b"hello").hexdigest(), hashlib.md5(b"HELLO").hexdigest())
        newer, _ = resolve(xml2, CFG.replace(b"6.4.3", b"6.4.5"))
        url = newer.archives[0].source.url.removeprefix(ROOT)
        server.objects[url] = b"PADHELLOxbye"
        count = len(server.requests)
        assert dd.sync(local_plan(newer, server), tmp_path) == {"downloaded": 1, "skipped": 1}
        assert server.requests[count:] == [url]
        assert (tmp_path / "data/a.bin").read_bytes() == b"HELLO"
    finally:
        server.close()


@pytest.mark.parametrize("kind", ["file-md5", "file-size", "pak-md5", "pak-size", "member-md5"])
def test_download_checks_sizes_and_hashes(tmp_path, kind, monkeypatch):
    plan, _ = resolve()
    objects = {
        plan.files[0].url.removeprefix(ROOT): b"MZ",
        plan.archives[0].source.url.removeprefix(ROOT): b"PADhelloxbye",
    }
    if kind == "file-md5":
        # Same size, wrong bytes: only the MD5 check can reject this.
        objects[plan.files[0].url.removeprefix(ROOT)] = b"NZ"
    elif kind == "file-size":
        # The right MD5 for bytes of another size: only the size check can reject this.
        plan = dataclasses.replace(
            plan, files=(dataclasses.replace(plan.files[0], size=3),)
        )
    elif kind == "pak-md5":
        # Keep every member intact; only the whole-object hash fails.
        objects[plan.archives[0].source.url.removeprefix(ROOT)] = b"BADhelloxbye"
    elif kind == "pak-size":
        # Members still match the bytes; only the declared whole-object size is wrong.
        archive = plan.archives[0]
        source = dataclasses.replace(archive.source, size=13)
        plan = dataclasses.replace(
            plan, archives=(dataclasses.replace(archive, source=source),)
        )
    elif kind == "member-md5":
        archive = plan.archives[0]
        member = dataclasses.replace(archive.members[1], md5="0" * 32)
        plan = dataclasses.replace(
            plan, archives=(dataclasses.replace(archive, members=(archive.members[0], member)),)
        )
    server = MemoryTransport(objects, monkeypatch)
    try:
        with pytest.raises(dd.DirectError):
            dd.sync(local_plan(plan, server), tmp_path)
    finally:
        server.close()
    # A failed install leaves no member, staged file or partial behind.
    assert not (tmp_path / "data" / "a.bin").exists()
    assert not (tmp_path / "data" / "b.bin").exists()
    assert not list(tmp_path.rglob("*.part"))
    assert not list(tmp_path.rglob("*.nadare-new"))
    if kind.startswith("file-"):
        assert not (tmp_path / plan.files[0].path).exists()


@pytest.mark.parametrize(
    "change",
    ["path", "member-md5", "member-size", "file", "missing", "record", "object-md5", "url"],
)
def test_cross_version_pak_reuse_requires_matching_content(tmp_path, monkeypatch, change):
    import json

    plan, _ = resolve()
    objects = {
        plan.files[0].url.removeprefix(ROOT): b"MZ",
        plan.archives[0].source.url.removeprefix(ROOT): b"PADhelloxbye",
    }
    server = MemoryTransport(objects, monkeypatch)
    plan = local_plan(plan, server)
    dd.sync(plan, tmp_path)
    arc = plan.archives[0]
    member = arc.members[0]
    if change == "path":
        member = dataclasses.replace(member, path="other.bin")
    elif change == "member-md5":
        member = dataclasses.replace(member, md5="0" * 32)
    elif change == "member-size":
        member = dataclasses.replace(member, size=4)
    elif change == "file":
        (tmp_path / member.path).write_bytes(b"BAD")
    elif change == "missing":
        (tmp_path / member.path).unlink()
    elif change == "record":
        record_path = tmp_path / dd.STATE_DIR / f"{arc.name}.done"
        record = json.loads(record_path.read_text())
        record["members"] = [{}]
        record_path.write_text(json.dumps(record))
    elif change == "object-md5":
        arc = dataclasses.replace(arc, source=dataclasses.replace(arc.source, md5="0" * 32))
    elif change == "url":
        arc = dataclasses.replace(
            arc, source=dataclasses.replace(arc.source, url=server.url + "/new")
        )
    arc = dataclasses.replace(arc, members=(member, *arc.members[1:]))
    updated = dataclasses.replace(plan, version="6.4.4", archives=(arc,))
    assert dd.plan_missing(updated, tmp_path) == ([], [arc])


@pytest.mark.parametrize("bad", [None, {}, 42, b"not a zip"])
def test_rejects_wrong_manifest_response_types(bad):
    with pytest.raises(dd.DirectError):
        tof.resolve(lambda url, headers: CFG if url == CONFIG else bad)
