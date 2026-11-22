#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check executable ELF hardening without external tools or section headers.

Usage: python3 scripts/check_elf_hardening.py FILE...
Shared libraries are deliberately not accepted as PIE executables.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path

#: Exact path -> (sha256, waived properties, reason) for a binary that cannot be hardened yet.
#: Empty since #1551 (7zzs is a static PIE now). An entry never waives NX and never matches
#: another digest or basename.
ALLOWLIST: dict[str, tuple[str, frozenset[str], str]] = {}


def inspect_elf(data: bytes) -> dict[str, bool]:
    """Return PIE/full RELRO/NX status; reject malformed or unsupported ELF."""
    if len(data) < 16 or data[:4] != b"\x7fELF":
        raise ValueError("not an ELF file")
    if data[4] not in (1, 2) or data[5] not in (1, 2) or data[6] != 1:
        raise ValueError("unsupported ELF class, encoding or version")
    wide = data[4] == 2
    endian = "<" if data[5] == 1 else ">"

    def unpack(fmt: str, offset: int):
        size = struct.calcsize(endian + fmt)
        if offset < 0 or offset + size > len(data):
            raise ValueError("truncated ELF structure")
        return struct.unpack_from(endian + fmt, data, offset)

    header = unpack("HHIQQQIHHHHHH" if wide else "HHIIIIIHHHHHH", 16)
    kind, _, version, entry, phoff, _, _, ehsize, phsize, phnum, *_ = header
    minimum_eh, minimum_ph = (64, 56) if wide else (52, 32)
    if version != 1 or ehsize != minimum_eh or phsize < minimum_ph or not phnum:
        raise ValueError("invalid ELF/program header sizes or version")
    if phnum == 0xFFFF:
        raise ValueError("extended program-header counts are unsupported")
    if phoff < ehsize or phoff + phsize * phnum > len(data):
        raise ValueError("truncated program header table")
    interp = relro = False
    stacks = []
    tags: dict[int, int] = {}
    for index in range(phnum):
        ph = unpack("IIQQQQQQ" if wide else "IIIIIIII", phoff + index * phsize)
        if wide:
            typ, flags, offset, _, _, size, memsize, _ = ph
        else:
            typ, offset, _, _, size, memsize, flags, _ = ph
        if offset + size > len(data) or size > memsize:
            raise ValueError("truncated or invalid program segment")
        if typ == 3:  # PT_INTERP
            if size < 2 or data[offset + size - 1] != 0:
                raise ValueError("invalid PT_INTERP")
            interp = True
        elif typ == 0x6474E552:  # PT_GNU_RELRO
            relro = True
        elif typ == 0x6474E551:  # PT_GNU_STACK; PF_X == 1
            stacks.append(not flags & 1)
        elif typ == 2:  # PT_DYNAMIC, independent of stripped section headers
            dynsize = 16 if wide else 8
            if size % dynsize:
                raise ValueError("invalid dynamic segment size")
            terminated = False
            for pos in range(offset, offset + size, dynsize):
                tag, value = unpack("qQ" if wide else "iI", pos)
                if tag == 0:  # DT_NULL
                    terminated = True
                    break
                tags[tag] = tags.get(tag, 0) | value
            if not terminated:
                raise ValueError("unterminated dynamic segment")
    flags1 = tags.get(0x6FFFFFFB, 0)  # DT_FLAGS_1
    static_pie = bool(entry and flags1 & 0x08000000 and 1 not in tags)
    now = bool(tags.get(30, 0) & 8 or flags1 & 1 or 24 in tags)
    return {
        "PIE": kind == 3 and (interp or static_pie),
        "full RELRO": relro and now,
        "NX stack": bool(stacks) and all(stacks),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = False
    for path in args.files:
        try:
            data = path.read_bytes()
            properties = inspect_elf(data)
            missing = {name for name, present in properties.items() if not present}
            reason = ""
            try:
                key = path.absolute().relative_to(Path.cwd()).as_posix()
            except ValueError:
                key = ""
            exception = ALLOWLIST.get(key)
            if missing and exception:
                digest, waived, explanation = exception
                if hashlib.sha256(data).hexdigest() == digest and missing <= waived:
                    reason = explanation
            status = "PASS" if not missing else "ALLOW" if reason else "FAIL"
            details = " ".join(f"{name}={'yes' if ok else 'no'}" for name, ok in properties.items())
            print(f"{status} {path}: {details}" + (f" ({reason})" if reason else ""))
            failed |= status == "FAIL"
        except (OSError, ValueError) as exc:
            print(f"FAIL {path}: {exc}")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
