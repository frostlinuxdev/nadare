#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Offline mutation fuzzer for Nadare's parsers of untrusted data (#812).

    scripts/fuzz.py --list
    scripts/fuzz.py --target sevenz --iterations 50000 --seed 7 --save build/fuzz

No network and no dependencies beyond Nadare itself; seeds come from ``tests/fixtures``. One
target per run, under an address-space cap. A **finding** is any exception that is not the
target's declared error type, a hang over ``--timeout`` seconds, a RecursionError or a
MemoryError (an allocation beyond the cap). Findings are printed, their inputs saved under
``--save``, and the exit code is 1. ``tests/test_fuzz_parsers.py`` runs every target briefly in
CI; run it longer by hand or on a schedule.

Lesson kept from the first harness: deep-copy every value inserted into a JSON seed, or shared
objects make cyclic structures JSON can never produce, and look like hangs.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import io
import json
import random
import resource
import signal
import struct
import sys
import traceback
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))  # the krpdiff builder lives with the tests


class Hang(Exception):
    pass


def _alarm(_signum, _frame):
    raise Hang("timeout")


# ------------------------------------------------------------------ mutators
def mutate_bytes(rng: random.Random, data: bytes) -> bytes:
    b = bytearray(data)
    for _ in range(rng.randint(1, 6)):
        if not b:
            b = bytearray(rng.randbytes(rng.randint(1, 16)))
        op = rng.random()
        i = rng.randrange(len(b))
        if op < 0.35:
            b[i] = rng.randrange(256)
        elif op < 0.5:
            b[i] ^= 1 << rng.randrange(8)
        elif op < 0.6:
            del b[i : i + rng.randint(1, 8)]
        elif op < 0.7:
            b[i:i] = rng.randbytes(rng.randint(1, 8))
        elif op < 0.8:
            b[i : i + 1] = rng.choice([b"\xff" * 9, b"\x00", b"\xff", b"\x80" * 9 + b"\x01"])
        elif op < 0.9 and len(b) > 8:
            j = rng.randrange(len(b) - 4)
            b[j : j + 4] = rng.choice([b"\xff\xff\xff\x7f", b"\x00\x00\x00\x80", b"\xff" * 4])
        else:
            b = b[: rng.randrange(len(b) + 1)]
    return bytes(b)


WEIRD = [
    None,
    True,
    False,
    0,
    -1,
    2**63,
    2**64,
    1e308,
    "",
    "A" * 5000,
    "\x00",
    "../../etc/passwd",
    "\\\\?\\unix\\etc",
    "C:\\x",
    "/abs",
    [],
    {},
    [[]],
    {"a": {"b": {}}},
    [None] * 1000,
    "http://x",
    "https://example.com/",
    {"$ref": 1},
    "0" * 64,
    "nan",
    -1e308,
]
KEYS = [
    "id",
    "url",
    "path",
    "name",
    "size",
    "md5",
    "sha256",
    "version",
    "files",
    "paks",
    "resource",
    "data",
    "hash",
    "text",
    "kinds",
    "link",
    "games",
    "archives",
    "parts",
]


def mutate_json(rng: random.Random, value):
    value = copy.deepcopy(value)

    def walk(x):
        if isinstance(x, dict):
            keys = list(x)
            if keys and rng.random() < 0.6:
                k = rng.choice(keys)
                r = rng.random()
                if r < 0.4:
                    x[k] = copy.deepcopy(rng.choice(WEIRD))
                elif r < 0.55:
                    del x[k]
                else:
                    walk(x[k])
            else:
                x[rng.choice(KEYS)] = copy.deepcopy(rng.choice(WEIRD))
        elif isinstance(x, list):
            if x and rng.random() < 0.6:
                i = rng.randrange(len(x))
                r = rng.random()
                if r < 0.4:
                    x[i] = copy.deepcopy(rng.choice(WEIRD))
                elif r < 0.5:
                    del x[i]
                else:
                    walk(x[i])
            else:
                x.append(copy.deepcopy(rng.choice(WEIRD)))

    for _ in range(rng.randint(1, 4)):
        if isinstance(value, (dict, list)):
            walk(value)
        else:
            value = copy.deepcopy(rng.choice(WEIRD))
    return value


def _json_or_bytes(rng: random.Random, raw: bytes) -> bytes:
    """Mostly structural JSON mutation, sometimes raw byte damage."""
    if rng.random() < 0.7:
        try:
            return json.dumps(mutate_json(rng, json.loads(raw))).encode()
        except (ValueError, TypeError, RecursionError):
            pass
    return mutate_bytes(rng, raw)


# ------------------------------------------------------------------- targets
@dataclass
class Target:
    fn: Callable[[object], object]
    errors: tuple[type[BaseException], ...]
    make: Callable[[random.Random], object]


def _fx(name: str) -> bytes:
    return (FIX / "catalog" / name).read_bytes()


def _sources_fetch(rng: random.Random, files: list[str]):
    """A fetch that answers each call with the next fixture, one of them damaged."""
    bodies = [_fx(f) for f in files]
    victim = rng.randrange(len(bodies))
    bodies[victim] = _json_or_bytes(rng, bodies[victim])
    calls = iter(bodies)

    def fetch(url, headers=None):
        try:
            return next(calls)
        except StopIteration:
            from nadare.core import directdl

            raise directdl.DirectError("no more responses") from None

    return fetch


def _seven_z_seeds() -> list[tuple[bytes, bytes, int]]:
    out = []
    for path in sorted((FIX / "sevenz").glob("*.7z"))[:8]:
        s = path.read_bytes()
        if len(s) < 32:
            continue
        off, size, _crc = struct.unpack("<QQI", s[12:32])
        out.append((s, s[32 + off : 32 + off + size], off))
    return out


def _seven_z(rng: random.Random) -> bytes:
    seeds = _seven_z_seeds()
    s, hdr, off = rng.choice(seeds)
    if hdr[:1] == b"\x01" and rng.random() < 0.8:
        # Re-sign the damaged header so it gets past the CRCs and reaches the parser.
        hdr = mutate_bytes(rng, hdr)
        start = struct.pack("<QQI", off, len(hdr), zlib.crc32(hdr) & 0xFFFFFFFF)
        out = bytearray(s[: 32 + off] + hdr)
        out[12:32] = start
        out[8:12] = struct.pack("<I", zlib.crc32(start) & 0xFFFFFFFF)
        return bytes(out)
    return mutate_bytes(rng, s)


def _sophon_seed() -> bytes:
    def varint(n):
        out = bytearray()
        while True:
            b, n = n & 0x7F, n >> 7
            out.append(b | 0x80 if n else b)
            if not n:
                return bytes(out)

    def v(num, n):
        return varint(num << 3) + varint(n)

    def ld(num, data):
        return varint((num << 3) | 2) + varint(len(data)) + data

    md5 = b"d41d8cd98f00b204e9800998ecf8427e"
    chunk = ld(1, b"chunk0") + ld(2, md5) + v(3, 0) + v(4, 60) + v(5, 100)
    entry = ld(1, b"dir/file.bin") + ld(2, chunk) + v(3, 0) + v(4, 200) + ld(5, md5)
    return ld(1, entry) + ld(1, ld(1, b"a.txt") + v(4, 5))


def _krdiff_seed() -> bytes:
    from _krpdiff import build

    old = [("Client/a.pak", bytes(range(200)))]
    new = [("Client/a.pak", b"N" * 30 + bytes(range(100))), ("Client/b.pak", b"x" * 40)]
    return build(old, new, [("new", b"N" * 30), ("old", 0, 100), ("new", b"x" * 40)])


def _vdf_seed() -> bytes:
    from nadare.core import shortcuts

    return shortcuts.vdf_dumps(
        [
            {
                "appid": 123,
                "AppName": "Game",
                "Exe": '"/x/run"',
                "StartDir": '"/x"',
                "tags": {"0": "a"},
            }
        ]
    )


_COMPAT = """"InstallConfigStore"
{
\t"Software"
\t{
\t\t"Valve"
\t\t{
\t\t\t"Steam"
\t\t\t{
\t\t\t\t"CompatToolMapping"
\t\t\t\t{
\t\t\t\t\t"123"
\t\t\t\t\t{
\t\t\t\t\t\t"name"\t\t"GE-Proton"
\t\t\t\t\t\t"config"\t\t""
\t\t\t\t\t\t"priority"\t\t"250"
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}
"""


def targets() -> dict[str, Target]:
    from nadare.core import (
        archivepins,
        artindex,
        directdl,
        krdiff,
        kurotokens,
        notices,
        sevenz,
        shortcuts,
        sophon,
        steamtools,
        yostar,
    )
    from nadare.core.sources import aethergazer, bd2, endfield, gfl2, mongil, wuwa

    blocks = base64.b64encode(hashlib.sha256(b"abc").digest()).decode()
    notice = {
        "notices": [
            {
                "id": "x-1",
                "text": "hi",
                "severity": "warn",
                "below": "0.1.0b11",
                "kinds": ["direct-endfield"],
                "link": "https://example.com/1",
            }
        ]
    }
    pins = {
        "schema": 1,
        "archives": {
            "a.zip": {
                "block_size": 1048576,
                "parts": [{"url": "https://example.com/a.zip", "size": 3, "blocks": blocks}],
            }
        },
    }
    art = {
        "schema": 1,
        "games": {
            "launcher-nikke": {
                "cover": [
                    {
                        "url": "https://nikke-en.com/a.jpg",
                        "width": 600,
                        "height": 900,
                        "orientation": "portrait",
                    }
                ]
            }
        },
    }
    ymanifest = {
        "game_tag": "G",
        "version": "1",
        "base_url": "https://cdn.yo-star.com/a",
        "generated_at": "2026-01-01T00:00:00Z",
        "files": [{"path": "a.exe", "size": 3, "crc64": 1}],
    }
    krd = _krdiff_seed()
    sop = _sophon_seed()
    vdf = _vdf_seed()
    dd = (directdl.DirectError,)

    def src(fn, files):
        return Target(lambda f: fn(f), dd, lambda rng: _sources_fetch(rng, files))

    return {
        "sevenz": Target(
            lambda d: sevenz.list_entries(io.BytesIO(d)),
            (sevenz.SevenZipError, EOFError),
            _seven_z,
        ),
        "sophon": Target(
            sophon.parse_manifest,
            (sophon.SophonError, ValueError),
            lambda rng: (
                mutate_bytes(rng, sop) if rng.random() < 0.8 else rng.randbytes(rng.randint(0, 200))
            ),
        ),
        "krdiff": Target(
            lambda d: krdiff.read_header(io.BytesIO(d)),
            (krdiff.KrDiffError,),
            lambda rng: mutate_bytes(rng, krd),
        ),
        # documented as tolerant: bad items are dropped, so nothing may escape
        "notices": Target(
            lambda d: notices.parse(d, url="https://example.com/n.json"),
            (),
            lambda rng: mutate_json(rng, notice),
        ),
        "archivepins": Target(
            archivepins.parse_document,
            (archivepins.PinError, ValueError),
            lambda rng: mutate_json(rng, pins),
        ),
        "artindex": Target(artindex.parse, (ValueError,), lambda rng: mutate_json(rng, art)),
        "kurotokens": Target(
            lambda d: kurotokens.parse(d, "pgr"),
            (ValueError,),
            lambda rng: mutate_bytes(
                rng, b'{"default":{"cdnList":[{"url":"https://x/"}]},"tokens":{}}'
            ),
        ),
        "yostar-manifest": Target(
            lambda d: yostar._parse_manifest(d, "fuzz"),
            (yostar.ManifestError,),
            lambda rng: mutate_json(rng, ymanifest),
        ),
        "shortcuts-vdf": Target(
            lambda d: shortcuts.vdf_loads(d, strict=True),
            (ValueError,),
            lambda rng: mutate_bytes(rng, vdf),
        ),
        "steam-config-vdf": Target(
            lambda t: (steamtools.read_compat_entry(t, "123"), steamtools.mapped_tools(t)),
            (),
            lambda rng: mutate_bytes(rng, _COMPAT.encode()).decode("utf-8", "replace"),
        ),
        "source-bd2": src(bd2.resolve, ["bd2_game.json"]),
        "source-aethergazer": src(aethergazer.resolve, ["ag_get_update_version.json"]),
        "source-mongil": src(mongil.resolve, ["mongil_builds.json"]),
        "source-endfield": src(endfield.resolve, ["endfield_get_latest.json"]),
        "source-gfl2": src(gfl2.resolve, ["gfl2_flexible_config.json", "gfl2_Version.txt"]),
        "source-wuwa": src(wuwa.resolve_legacy, ["wuwa_index.json", "wuwa_indexFile.json"]),
    }


# ----------------------------------------------------------------------- run
def fuzz(name: str, target: Target, iterations: int, seed: int, timeout: float, save: Path | None):
    rng = random.Random(f"{name}:{seed}")
    findings: dict[str, int] = {}
    for i in range(iterations):
        data = target.make(rng)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            target.fn(data)
        except target.errors:
            pass
        except (Hang, MemoryError, RecursionError) as exc:
            key = type(exc).__name__
            _record(findings, key, name, i, data, save)
        except Exception as exc:  # noqa: BLE001 - this is what we are hunting
            tb = traceback.extract_tb(exc.__traceback__)[-1]
            key = f"{type(exc).__name__} at {Path(tb.filename).name}:{tb.lineno}: {str(exc)[:80]}"
            _record(findings, key, name, i, data, save)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    return findings


def _record(findings, key, name, i, data, save):
    first = key not in findings
    findings[key] = findings.get(key, 0) + 1
    if first and save is not None and isinstance(data, (bytes, str)):
        save.mkdir(parents=True, exist_ok=True)
        raw = data if isinstance(data, bytes) else data.encode("utf-8", "surrogateescape")
        (save / f"{name}-{i}.bin").write_bytes(gzip.compress(raw))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true", help="print the target names")
    ap.add_argument("--target", help="one target (see --list)")
    ap.add_argument("--iterations", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=3.0, help="seconds per input")
    ap.add_argument("--memory-gb", type=float, default=3.0, help="address-space cap")
    ap.add_argument("--save", type=Path, help="folder for the inputs of new findings")
    args = ap.parse_args(argv)
    if args.list:
        print("\n".join(targets()))
        return 0
    if not args.target:
        ap.error("--target is required (or --list)")
    cap = int(args.memory_gb * 1024**3)
    resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
    signal.signal(signal.SIGALRM, _alarm)
    found = fuzz(
        args.target, targets()[args.target], args.iterations, args.seed, args.timeout, args.save
    )
    print(
        json.dumps(
            {
                "target": args.target,
                "iterations": args.iterations,
                "seed": args.seed,
                "findings": found,
            },
            indent=2,
        )
    )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
