# SPDX-License-Identifier: GPL-3.0-or-later
"""Build tiny uncompressed .cab files and a Burn-like bundle so 7-Zip can really unpack them."""

from __future__ import annotations

import struct


def make_cab(files: dict[str, bytes]) -> bytes:
    """One folder, no compression (type 0), data cut into 32 KiB blocks as the format wants."""
    names = list(files)
    blob = b"".join(files[n] for n in names)
    blocks = [blob[i : i + 32768] for i in range(0, len(blob), 32768)] or [b""]
    file_entries = b""
    offset = 0
    for name in names:
        data = files[name]
        file_entries += struct.pack("<IIHHHH", len(data), offset, 0, 0x5A21, 0x0000, 0x20)
        file_entries += name.encode("ascii") + b"\0"
        offset += len(data)
    coff_files = 36 + 8
    coff_data = coff_files + len(file_entries)
    folder = struct.pack("<IHH", coff_data, len(blocks), 0)
    data_blocks = b"".join(struct.pack("<IHH", 0, len(b), len(b)) + b for b in blocks)
    total = coff_data + len(data_blocks)
    header = struct.pack(
        "<4sIIIIIBBHHHHH",
        b"MSCF", 0, total, 0, coff_files, 0, 3, 1, 1, len(names), 0, 0, 0,
    )
    return header + folder + file_entries + data_blocks


def make_pe(
    machine: int = 0x8664, size: int = 6000, version: tuple[int, int, int, int] | None = None
) -> bytes:
    """The smallest thing that looks like a PE: MZ, e_lfanew, 'PE\\0\\0', machine.

    ``version`` plants a VS_FIXEDFILEINFO (signature 0xFEEF04BD) far into the file, where a
    real DLL's version resource sits.
    """
    body = bytearray(size)
    body[0:2] = b"MZ"
    struct.pack_into("<I", body, 0x3C, 0x80)
    body[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", body, 0x84, machine)
    if version is not None:
        major, minor, build, rev = version
        struct.pack_into(
            "<IIII", body, 0x400, 0xFEEF04BD, 0x00010000, (major << 16) | minor, (build << 16) | rev
        )
    return bytes(body)


def make_bundle(inner: dict[str, bytes], *, ux: dict[str, bytes] | None = None) -> bytes:
    """A Burn-like exe: junk, a UX cab, junk, then the package cab that holds nested cabs."""
    ux_cab = make_cab(ux or {"u0": b"ux-not-a-dll"})
    pkg = make_cab({"a0": b"an-msi", "a12": make_cab(inner)})
    return b"MZ" + b"\0" * 5000 + ux_cab + b"\0" * 777 + pkg + b"\0" * 300
