# SPDX-License-Identifier: GPL-3.0-or-later
"""Hardening checks use synthetic ELF images and optional tiny compiler fixtures."""

import hashlib
import importlib.util
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_elf_hardening.py"


def load():
    spec = importlib.util.spec_from_file_location("check_elf_hardening", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def elf(
    *,
    bits=64,
    endian="<",
    kind=3,
    interp=True,
    relro=True,
    stack=6,
    dynamic=((30, 8),),
    entry=0x1000,
):
    """No section table: dynamic entries must be read through PT_DYNAMIC."""
    wide = bits == 64
    ehsize, phsize = (64, 56) if wide else (52, 32)
    dyn = b"".join(struct.pack(endian + ("qQ" if wide else "iI"), *d) for d in (*dynamic, (0, 0)))
    segments = [(2, 6, dyn)]
    if interp:
        segments.append((3, 4, b"/ld.so\0"))
    if relro:
        segments.append((0x6474E552, 4, b""))
    if stack is not None:
        segments.append((0x6474E551, stack, b""))
    ident = b"\x7fELF" + bytes([2 if wide else 1, 1 if endian == "<" else 2, 1])
    header = (
        ident
        + bytes(9)
        + struct.pack(
            endian + ("HHIQQQIHHHHHH" if wide else "HHIIIIIHHHHHH"),
            kind,
            62 if wide else 3,
            1,
            entry,
            ehsize,
            0,
            0,
            ehsize,
            phsize,
            len(segments),
            0,
            0,
            0,
        )
    )
    offset = ehsize + phsize * len(segments)
    headers, blobs = [], []
    for typ, flags, blob in segments:
        values = (
            (typ, flags, offset, 0, 0, len(blob), len(blob), 1)
            if wide
            else (typ, offset, 0, 0, len(blob), len(blob), flags, 1)
        )
        headers.append(struct.pack(endian + ("IIQQQQQQ" if wide else "IIIIIIII"), *values))
        blobs.append(blob)
        offset += len(blob)
    return header + b"".join(headers) + b"".join(blobs)


@pytest.mark.parametrize("bits,endian", [(32, "<"), (32, ">"), (64, "<"), (64, ">")])
@pytest.mark.parametrize("dynamic", [((30, 8),), ((0x6FFFFFFB, 1),), ((24, 0),)])
def test_hardened_elf_without_sections(bits, endian, dynamic):
    assert load().inspect_elf(elf(bits=bits, endian=endian, dynamic=dynamic)) == {
        "PIE": True,
        "full RELRO": True,
        "NX stack": True,
    }


@pytest.mark.parametrize(
    "kwargs,missing",
    [
        ({"kind": 2}, "PIE"),
        ({"relro": False}, "full RELRO"),
        ({"dynamic": ()}, "full RELRO"),
        ({"stack": 7}, "NX stack"),
        ({"stack": None}, "NX stack"),
        ({"interp": False}, "PIE"),
        ({"interp": False, "entry": 0, "dynamic": ((0x6FFFFFFB, 0x8000001),)}, "PIE"),
    ],
)
def test_missing_properties(kwargs, missing):
    assert load().inspect_elf(elf(**kwargs))[missing] is False


def test_static_pie_requires_pie_flag_and_no_needed_library():
    module = load()
    flags = ((0x6FFFFFFB, 0x8000001),)
    assert module.inspect_elf(elf(interp=False, dynamic=flags))["PIE"]
    assert not module.inspect_elf(elf(interp=False, dynamic=(*flags, (1, 1))))["PIE"]


@pytest.mark.parametrize(
    "blob", [b"", b"not ELF", elf()[:63], elf()[:-1], elf()[:5] + b"\x03" + elf()[6:]]
)
def test_malformed_elf_is_rejected(blob):
    with pytest.raises(ValueError):
        load().inspect_elf(blob)


def test_cli_checks_all_files_and_fails_closed(tmp_path):
    good, bad, broken = (tmp_path / name for name in ("good", "bad", "broken"))
    good.write_bytes(elf())
    bad.write_bytes(elf(kind=2, dynamic=(), stack=7))
    broken.write_bytes(b"bad")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(good), str(bad), str(broken)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert f"PASS {good}: PIE=yes full RELRO=yes NX stack=yes" in result.stdout
    assert f"FAIL {bad}: PIE=no full RELRO=no NX stack=no" in result.stdout
    assert f"FAIL {broken}:" in result.stdout


def test_allowlist_is_path_digest_and_property_scoped(tmp_path, monkeypatch, capsys):
    module = load()
    target = tmp_path / "usr/bin/_internal/nadare/assets/7zzs"
    target.parent.mkdir(parents=True)
    target.write_bytes(elf(kind=2, dynamic=()))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        module,
        "ALLOWLIST",
        {
            "usr/bin/_internal/nadare/assets/7zzs": (
                hashlib.sha256(target.read_bytes()).hexdigest(),
                frozenset({"PIE", "full RELRO"}),
                "test exception",
            ),
        },
    )
    assert module.main([str(target)]) == 0
    assert "ALLOW" in capsys.readouterr().out
    target.write_bytes(elf(kind=2, dynamic=(), stack=7))
    assert module.main([str(target)]) == 1
    # Even an otherwise authorized new digest cannot waive NX.
    module.ALLOWLIST["usr/bin/_internal/nadare/assets/7zzs"] = (
        hashlib.sha256(target.read_bytes()).hexdigest(),
        frozenset({"PIE", "full RELRO"}),
        "test exception",
    )
    assert module.main([str(target)]) == 1
    other = tmp_path / "7zzs"
    other.write_bytes(elf(kind=2, dynamic=()))
    assert module.main([str(other)]) == 1


@pytest.mark.parametrize("hardened", [False, True])
def test_compiler_binaries(tmp_path, hardened):
    # The host's ~/.local/bin/cc is a terminal helper, not a C compiler.
    cc = shutil.which("cc", path="/usr/bin:/bin")
    if cc is None:
        pytest.skip("cc is not installed")
    source, binary = tmp_path / "tiny.c", tmp_path / "tiny"
    source.write_text('#include <stdio.h>\nint main(void) { puts("ok"); return 0; }\n')
    flags = (
        ["-fPIE", "-pie", "-Wl,-z,relro,-z,now,-z,noexecstack"]
        if hardened
        else ["-fno-pie", "-no-pie", "-Wl,-z,norelro,-z,lazy,-z,execstack"]
    )
    subprocess.run([cc, str(source), "-o", str(binary), *flags], check=True, capture_output=True)
    assert set(load().inspect_elf(binary.read_bytes()).values()) == {hardened}


def test_compiler_static_pie(tmp_path):
    cc = shutil.which("cc", path="/usr/bin:/bin")
    if cc is None:
        pytest.skip("cc is not installed")
    source, binary = tmp_path / "tiny.c", tmp_path / "tiny"
    source.write_text("int main(void) { return 0; }\n")
    result = subprocess.run(
        [
            cc,
            str(source),
            "-o",
            str(binary),
            "-fPIE",
            "-static-pie",
            "-Wl,-z,relro,-z,now,-z,noexecstack",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        pytest.skip(f"compiler/linker has no static-pie support: {result.stderr}")
    assert all(load().inspect_elf(binary.read_bytes()).values())


@pytest.mark.parametrize("mutation", ["phoff", "phsize", "segment", "dynamic"])
def test_malformed_program_headers(mutation):
    data = bytearray(elf())
    if mutation == "phoff":
        struct.pack_into("<Q", data, 32, len(data) + 1)
    elif mutation == "phsize":
        struct.pack_into("<H", data, 54, 1)
    elif mutation == "segment":
        struct.pack_into("<Q", data, 64 + 8, len(data) + 1)
    else:
        # Replace DT_NULL with a non-terminating tag in the first segment.
        dynoff = struct.unpack_from("<Q", data, 64 + 8)[0]
        struct.pack_into("<q", data, dynoff + 16, 30)
    with pytest.raises(ValueError):
        load().inspect_elf(data)


def test_no_binary_is_waived_any_more():
    """#1551: 7zzs is static-pie now; the old exception for its ET_EXEC build is gone."""
    assert load().ALLOWLIST == {}
