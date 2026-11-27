# SPDX-License-Identifier: GPL-3.0-or-later
"""Small published excerpts for Strinova's published text manifests."""

from pathlib import Path

import pytest

from nadare.core import directdl as dd
from nadare.core.sources import strinova

FIX = Path(__file__).parent / "fixtures/catalog"
BASE = "https://resource-download.strinova.com/Client/Win/GameDepot"
INDEX = (FIX / "strinova_index.txt").read_bytes()
FILES = (FIX / "strinova_files.txt").read_bytes()


def resolve(index=INDEX, files=FILES, installed=None):
    calls = []

    def fetch(url, headers=None):
        calls.append((url, headers))
        return {
            BASE + "/Game_Release/manifest.txt": index,
            BASE + "/Game_Release/1.12.1.2/manifest.txt": files,
        }[url]

    return strinova.resolve(fetch, installed=installed), calls


def test_fresh_plan():
    plan, calls = resolve()
    assert calls == [
        (BASE + "/Game_Release/manifest.txt", None),
        (BASE + "/Game_Release/1.12.1.2/manifest.txt", None),
    ]
    assert (plan.game_tag, plan.version, plan.exe) == ("strinova", "1.12.1.2", "Strinova.exe")
    assert plan.files == ()
    assert len(plan.archives) == 2
    assert sum(a.source.size for a in plan.archives) == 271786499
    for arc, path, md5, size in zip(
        plan.archives,
        ["1.12.1.2/full_zip/MinZipSingle86.7z", "1.11.4.8/full_zip/MultFile2.7z"],
        ["385f249222efe6ba833e89efbc6b2996", "9276c72c673ca577ead2a62ac9ac71dc"],
        [220796060, 50990439],
        strict=True,
    ):
        assert arc.source.url == BASE + "/Game_Release/" + path
        assert (arc.source.md5, arc.source.size, arc.unwrap) == (md5, size, ("7z",))
        dd.safe_relative_path(arc.name)
        dd.safe_relative_path(arc.source.path)
    assert plan.extra["verification_files"] == (
        {"path": "Strinova.exe", "md5": "016fbf29f3aef0a2a6fd2fdb564a56ed", "size": 769376},
        {
            "path": "Engine/Binaries/ThirdParty/DbgHelp/dbghelp.dll",
            "md5": "15ee5c7404fa5b6de0eb0c042474d3bf",
            "size": 1369936,
        },
    )


@pytest.mark.parametrize(
    "installed,count,total",
    [
        ("1.11.5.10", 1, 1258937798),
        ("1.10.0.0", 2, 271786499),
        ("1.12.1.2", 2, 271786499),
    ],
)
def test_update_selection(installed, count, total):
    plan, _ = resolve(installed=installed)
    assert len(plan.archives) == count
    assert sum(a.source.size for a in plan.archives) == total
    if count == 1:
        assert plan.archives[0].source.url == BASE + "/Game_Release/1.12.1.2/patch_zip/1.11.5.10.7z"


@pytest.mark.parametrize("document", ["index", "files"])
@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        "text",
        b"\xff",
        b"",
        b"incomplete|row",
        b"/absolute|" + b"a" * 32 + b"|1",
        b"../escape|" + b"a" * 32 + b"|1",
        b"Strinova.exe|bad|1",
        b"Strinova.exe|" + b"a" * 32 + b"|-1",
        b"Strinova.exe|" + b"a" * 32 + b"|1.5",
    ],
)
def test_broken_documents(document, bad):
    with pytest.raises(dd.DirectError):
        resolve(**{document: bad})


@pytest.mark.parametrize(
    "old,new",
    [
        (b"--last_version--\n1.12.1.2", b""),
        (b"--last_version--\n1.12.1.2", b"--last_version--\n../../bad"),
        (b"--last_version--\n1.12.1.2", b"--last_version--\n1.12.1.2\n--last_version--\n2"),
        (b"/Game_Release/1.12.1.2/full_zip/MinZipSingle86.7z", b"/Game_Release/../escape.7z"),
        (b"/Game_Release/1.12.1.2/full_zip/MinZipSingle86.7z", b"//evil.test/a.7z"),
        (b"/Game_Release/1.12.1.2/full_zip/MinZipSingle86.7z", b"/Other/a.7z"),
        (b"385f249222efe6ba833e89efbc6b2996", b"bad"),
        (b"220796060", b"-1"),
        (b"220796060", b"true"),
        (b"1.12.1.2->1.12.1.2", b"broken"),
        (b"1.12.1.2->1.12.1.2", b"1.12.1.2->../bad"),
        (b"1.12.1.2->1.12.1.2", b"1->2"),
    ],
)
def test_bad_index_entries(old, new):
    with pytest.raises(dd.DirectError):
        resolve(index=INDEX.replace(old, new))


@pytest.mark.parametrize("document", ["index", "files"])
def test_duplicates(document):
    body = INDEX if document == "index" else FILES
    row = next(line for line in body.splitlines() if b"|" in line)
    with pytest.raises(dd.DirectError):
        resolve(**{document: body + row + b"\n"})


def test_ambiguous_incremental():
    with pytest.raises(dd.DirectError):
        resolve(
            index=INDEX + INDEX.splitlines()[-1].replace(b"1.11.5.10.7z", b"other.7z") + b"\n",
            installed="1.11.5.10",
        )


@pytest.mark.parametrize(
    "installed,count,total", [(None, 2, 271786499), ("1.11.5.10", 1, 1258937798)]
)
def test_published_index_excerpt(installed, count, total):
    lines = INDEX.splitlines()
    assert lines[:3] == [b"--last_version--", b"1.12.1.2", b"--download_version--"]
    assert len(lines[3:]) == 20
    assert sum(b"->1.12.1.2|" not in line for line in lines[3:]) == 17
    plan, _ = resolve(installed=installed)
    assert plan.version == "1.12.1.2"
    assert len(plan.archives) == count
    assert sum(arc.source.size for arc in plan.archives) == total
