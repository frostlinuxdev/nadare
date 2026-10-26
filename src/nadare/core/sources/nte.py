# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Direct-download source for Neverness to Everness (global).

Perfect World's patcher publishes a ``ResList.bin`` per version: the magic
``PatcherXML0``, a little-endian u32 with the XML's length, then AES-128-CBC of the
zlib-compressed XML. The key is ``"<appId>@Patcher"`` and the IV is ``"PatcherSDK"``,
both right-padded with ``"0"`` to 16 bytes (the app id is in the public entry config).
Nothing in it is a secret: the launcher derives it from public values.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from typing import Any
from urllib.parse import urlparse

from .. import directdl
from . import Fetch, _aes, default_fetch

ENTRY_CONFIG_URL = "https://ntecdn1.perfectworld.com/clientRes/publish_PC/Version/Windows/config.xml"
MAGIC = b"PatcherXML0\x00"
EXE = "Client/WindowsNoEditor/HT/Binaries/Win64/HTGame.exe"

_MAX_XML = 64 * 1024 * 1024
_MAX_MANIFEST = 16 * 1024 * 1024
_MD5 = re.compile(r"[0-9a-fA-F]{32}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_BRANCH = re.compile(r"[A-Za-z0-9_]+")


def _int(value: Any, what: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise directdl.DirectError(f"invalid {what}: {value!r}") from exc
    if number < 0:
        raise directdl.DirectError(f"negative {what}: {value!r}")
    return number


def _md5(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _MD5.fullmatch(value):
        raise directdl.DirectError(f"invalid md5 for {what}: {value!r}")
    return value.lower()


def decode_container(data: bytes, app_id: str) -> str:
    """Decrypt and inflate a ``PatcherXML0`` container to its XML text.

    Raises :class:`directdl.DirectError` for a wrong magic, bad padding, a failed
    inflate, or an inflated length that differs from the stored one.
    """
    if not isinstance(data, bytes) or len(data) < 32 or data[:12] != MAGIC:
        raise directdl.DirectError("not a PatcherXML0 container")
    (stored,) = struct.unpack("<I", data[12:16])
    if stored > _MAX_XML:
        raise directdl.DirectError("PatcherXML0 manifests XML is too large")
    body = data[16:]
    if not body or len(body) % 16:
        raise directdl.DirectError("PatcherXML0 ciphertext has an invalid length")

    key = (str(app_id) + "@Patcher").ljust(16, "0")[:16].encode("ascii", "strict")
    iv = b"PatcherSDK".ljust(16, b"0")
    try:
        plain = _aes.pkcs7_unpad(_aes.aes128_cbc_decrypt(key, iv, body))
        stream = zlib.decompressobj()
        inflated = stream.decompress(plain, stored)
        if stream.unconsumed_tail or not stream.eof or stream.unused_data:
            raise directdl.DirectError("PatcherXML0 payload is larger than declared")
        if len(inflated) != stored:
            raise directdl.DirectError("PatcherXML0 length does not match its header")
        return inflated.decode("utf-8")
    except directdl.DirectError:
        raise
    except (ValueError, TypeError, zlib.error, UnicodeError) as exc:
        raise directdl.DirectError(f"cannot decode PatcherXML0 container: {exc}") from exc


def _xml(data: bytes, what: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError, TypeError) as exc:
        raise directdl.DirectError(f"{what} is not valid XML") from exc


def plan_from_xml(xml: str, res_url: str, version: str) -> directdl.Plan:
    """Build the plan for the base tag from a decoded ResList.

    ``res_url`` is the CDN root of the branch (``.../clientRes/PC_140``). Top-level
    ``<Res>`` elements become whole files at ``Res/<md5[0]>/<md5>.<size>``; each
    ``<Package><Pak>`` becomes a ``("slices",)`` archive whose ``<Entry>`` records
    are the members (``offset``/``size``/``md5``). ``<BaseVersion>`` (optional tags)
    are left out, and so is every path listed in ``<Ignored>``. Raises
    :class:`directdl.DirectError` on malformed or hostile input.
    """
    if urlparse(res_url).scheme != "https":
        raise directdl.DirectError(f"refusing a non-https URL: {res_url!r}")
    if not isinstance(xml, str) or len(xml) > _MAX_MANIFEST:
        raise directdl.DirectError("ResList is too large or not text")
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise directdl.DirectError("refusing a ResList with entity declarations")
    root = _xml(xml.encode("utf-8"), "ResList")
    root_url = res_url.rstrip("/")

    # Files the client writes itself (server lists, launcher config): the patcher
    # lists them in the manifest but never installs or checks them.
    ignored = {
        item.get("name")
        for element in root
        if element.tag == "Ignored"
        for item in element.findall("Item")
    }

    files: list[directdl.RemoteFile] = []
    archives: list[directdl.Archive] = []
    for element in root:
        if element.tag == "Res":
            filename = element.get("filename")
            if filename in ignored:
                continue
            md5 = _md5(element.get("md5"), "Res")
            size = _int(element.get("filesize"), "filesize")
            path = directdl.safe_relative_path(filename)  # type: ignore[arg-type]
            files.append(
                directdl.RemoteFile(
                    path=path,
                    url=f"{root_url}/Res/{md5[0]}/{md5}.{size}",
                    size=size,
                    md5=md5,
                )
            )
        elif element.tag == "Package":
            for pak in element.findall("Pak"):
                md5 = _md5(pak.get("md5"), "Pak")
                size = _int(pak.get("filesize"), "filesize")
                name = f"pak-{md5}"
                members: list[directdl.Member] = []
                for entry in pak.findall("Entry"):
                    entry_name = entry.get("name")
                    if entry_name in ignored:
                        continue
                    members.append(
                        directdl.Member(
                            path=directdl.safe_relative_path(entry_name),  # type: ignore[arg-type]
                            offset=_int(entry.get("offset"), "offset"),
                            size=_int(entry.get("size"), "size"),
                            md5=_md5(entry.get("md5"), "Entry"),
                        )
                    )
                if not members:
                    continue
                archives.append(
                    directdl.Archive(
                        name=name,
                        source=directdl.RemoteFile(
                            path=name,
                            url=f"{root_url}/Res/{md5[0]}/{md5}.{size}",
                            size=size,
                            md5=md5,
                        ),
                        unwrap=("slices",),
                        members=tuple(members),
                    )
                )

    return directdl.Plan(
        game_tag="nte",
        version=version,
        files=tuple(files),
        archives=tuple(archives),
        exe=EXE,
    )


def _entry_config(data: bytes) -> tuple[str, str, str]:
    root = _xml(data, "entry config")
    node = root.find("ResConfig")
    text = (node.text or "").strip() if node is not None else ""
    if not text:
        raise directdl.DirectError("entry config is missing ResConfig")
    try:
        decoded = base64.b64decode(text, validate=True)
        value = json.loads(decoded.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeError) as exc:
        raise directdl.DirectError("ResConfig is not valid base64 JSON") from exc
    if not isinstance(value, dict):
        raise directdl.DirectError("ResConfig is not a JSON object")

    cdns = value.get("gameResUrl")
    if (
        not isinstance(cdns, list)
        or not cdns
        or not all(isinstance(url, str) and urlparse(url).scheme == "https" for url in cdns)
    ):
        raise directdl.DirectError("ResConfig is missing an https gameResUrl")
    branch = value.get("branchName")
    if not isinstance(branch, str) or not _BRANCH.fullmatch(branch):
        raise directdl.DirectError("ResConfig has an invalid branchName")
    app_id = value.get("appId")
    if isinstance(app_id, bool) or not isinstance(app_id, (str, int)):
        raise directdl.DirectError("ResConfig has an invalid appId")
    app_id = str(app_id)
    if not app_id.isdigit():
        raise directdl.DirectError("ResConfig has a non-numeric appId")
    res_url = cdns[0].rstrip("/") + "/" + branch
    return res_url, app_id, branch


def _manifest_zip(data: bytes, what: str) -> bytes:
    limit = 16 * 1024 * 1024
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            info = zf.getinfo("ResList.bin")
            if info.file_size > limit:
                raise directdl.DirectError(f"{what} member is too large")
            with zf.open(info) as member:
                body = member.read(limit + 1)
    except directdl.DirectError:
        raise
    except (KeyError, OSError, EOFError, ValueError, zipfile.BadZipFile) as exc:
        raise directdl.DirectError(f"{what} is not a valid zip with ResList.bin") from exc
    if len(body) > limit:
        raise directdl.DirectError(f"{what} member is too large")
    return body


def resolve(fetch: Fetch | None = None) -> directdl.Plan:
    fetch = fetch or default_fetch
    res_url, app_id, _branch = _entry_config(fetch(ENTRY_CONFIG_URL, None))

    branch_xml = _xml(fetch(f"{res_url}/Version/Windows/config.xml", None), "branch config")
    node = branch_xml.find("ResVersion")
    version = (node.text or "").strip() if node is not None else ""
    if not _VERSION.fullmatch(version):
        raise directdl.DirectError("branch config has an invalid ResVersion")

    archive = fetch(f"{res_url}/Version/Windows/version/{version}/ResList.bin.zip", None)
    container = _manifest_zip(archive, "ResList.bin.zip")
    xml = decode_container(container, app_id)
    return plan_from_xml(xml, res_url, version)
