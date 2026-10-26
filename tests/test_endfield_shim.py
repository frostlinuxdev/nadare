# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for nadare.core.endfield_shim (no compiler, 7z or network needed)."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from nadare.core import endfield_shim as es
from nadare.core import sevenzip


@pytest.fixture(autouse=True)
def no_bundled_7zip(tmp_path, monkeypatch):
    """Unit tests must never pick up the real bundled 7-Zip a release build fetched."""
    monkeypatch.setattr(sevenzip, "BUNDLED", tmp_path / "no-bundled-7zzs")

MZ = b"MZ" + b"\0" * 2048


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://launcher.hg-cdn.com/CNKz/launcher/GryphlineLauncher.exe", True),
        ("https://hg-cdn.com/x.exe", True),
        ("https://cdn.gryphline.com/x.exe", True),
        ("https://gryphline.com/x.exe", True),
        ("http://launcher.hg-cdn.com/x.exe", False),
        ("https://evil-hg-cdn.com/x.exe", False),
        ("https://hg-cdn.com.evil.example/x.exe", False),
        ("https://hg-cdn.com@evil.example/x.exe", False),
        ("file:///tmp/hg-cdn.com/x.exe", False),
    ],
)


def test_installer_host_allowed_checks_the_real_hostname(url, allowed):
    assert es.installer_host_allowed(url) is allowed


def test_win_path_inside_the_prefix_uses_c_drive(tmp_path):
    prefix = tmp_path / "pfx"
    p = prefix / "drive_c" / "Games" / "A B" / "Endfield.exe"
    assert es.win_path(prefix, p) == "C:\\Games\\A B\\Endfield.exe"


def test_win_path_outside_the_prefix_uses_z_drive(tmp_path):
    prefix = tmp_path / "pfx"
    assert es.win_path(prefix, Path("/mnt/nas/g/Endfield.exe")) == "Z:\\mnt\\nas\\g\\Endfield.exe"


def test_is_steam_build_looks_for_steam_api(tmp_path):
    assert not es.is_steam_build(tmp_path)
    (tmp_path / "steam_api64.dll").write_bytes(b"x")
    assert es.is_steam_build(tmp_path)


def test_launch_argv_shape():
    assert es.launch_argv(
        "C:\\s\\suspend_start.exe", "C:\\s\\Games.exe", "C:\\g\\Endfield.exe", ["-a", "b c"]
    ) == [
        "C:\\s\\suspend_start.exe",
        "--launcher",
        "C:\\s\\Games.exe",
        "C:\\g\\Endfield.exe",
        "-a",
        "b c",
    ]


def test_find_games_exe_picks_newest_version_and_none_when_missing(tmp_path):
    prefix = tmp_path / "pfx"
    assert es.find_games_exe(prefix) is None
    for v in ("0.1.9", "0.1.25", "0.1.3"):
        d = prefix / "drive_c" / "Program Files" / "GRYPHLINK" / v
        d.mkdir(parents=True)
        (d / "Games.exe").write_bytes(MZ)
    assert es.find_games_exe(prefix).parent.name == "0.1.25"  # numeric, not lexical, order


def _fake_cc(calls, fail=False):
    def run(argv, **kw):
        calls.append(argv)
        if fail:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        out = Path(argv[argv.index("-o") + 1])
        out.write_bytes(MZ)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def test_build_shim_compiles_then_reuses_until_the_source_changes(tmp_path):
    src = tmp_path / "s.c"
    src.write_text("int main(){}")
    out = tmp_path / "out" / "suspend_start.exe"
    calls: list = []
    es.build_shim(
        out, source=src, cc="fake-gcc", which=lambda c: "/usr/bin/" + c, run=_fake_cc(calls)
    )
    assert out.read_bytes() == MZ and len(calls) == 1
    flags = calls[0]
    assert (
        flags[0] == "/usr/bin/fake-gcc"
        and "-municode" in flags
        and "-mwindows" in flags
        and str(src) in flags
    )
    es.build_shim(
        out, source=src, cc="fake-gcc", which=lambda c: "/usr/bin/" + c, run=_fake_cc(calls)
    )
    assert len(calls) == 1  # same source: not rebuilt
    src.write_text("int main(){return 1;}")
    es.build_shim(
        out, source=src, cc="fake-gcc", which=lambda c: "/usr/bin/" + c, run=_fake_cc(calls)
    )
    assert len(calls) == 2


def test_build_shim_without_a_compiler_explains_what_to_install(tmp_path):
    src = tmp_path / "s.c"
    src.write_text("x")
    with pytest.raises(es.ShimError, match="mingw"):
        es.build_shim(
            tmp_path / "o.exe", source=src, cc=None, which=lambda c: None, run=_fake_cc([])
        )


def test_build_shim_reports_compiler_errors(tmp_path):
    src = tmp_path / "s.c"
    src.write_text("x")
    with pytest.raises(es.ShimError, match="boom"):
        es.build_shim(
            tmp_path / "o.exe", source=src, cc="g", which=lambda c: c, run=_fake_cc([], fail=True)
        )
    assert not (tmp_path / "o.exe").exists()


def _fake_7z(calls, games=MZ, place=True):
    def run(argv, **kw):
        calls.append(argv)
        outdir = Path(next(a for a in argv if a.startswith("-o"))[2:])
        if place:
            d = outdir / "$0" / "0.1.25"
            d.mkdir(parents=True)
            (d / "Games.exe").write_bytes(games)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def test_extract_games_exe_pulls_only_that_file_and_checks_it_is_a_pe(tmp_path):
    inst = tmp_path / "GryphlineLauncher.exe"
    inst.write_bytes(b"nsis")
    calls: list = []
    dest = tmp_path / "cache" / "Games.exe"
    got = es.extract_games_exe(
        inst, dest, which=lambda c: "/usr/bin/7z" if c == "7z" else None, run=_fake_7z(calls)
    )
    assert got == dest and dest.read_bytes() == MZ
    assert calls[0][:2] == ["/usr/bin/7z", "x"]
    assert "Games.exe" in calls[0] and str(inst) in calls[0]


def test_extract_games_exe_uses_bundled_7zip_when_system_missing(tmp_path, monkeypatch):
    inst = tmp_path / "GryphlineLauncher.exe"
    inst.write_bytes(b"nsis")
    calls = []
    monkeypatch.setattr(es.sevenzip, "find", lambda *, which: "/cache/bin/7zzs-test")
    got = es.extract_games_exe(inst, tmp_path / "Games.exe", which=lambda _: None,
                               run=_fake_7z(calls))
    assert got.read_bytes() == MZ and calls[0][0] == "/cache/bin/7zzs-test"


def test_extract_games_exe_rejects_non_pe_and_missing_7z_and_missing_file(tmp_path):
    inst = tmp_path / "i.exe"
    inst.write_bytes(b"nsis")
    with pytest.raises(es.ShimError, match="not a Windows executable"):
        es.extract_games_exe(
            inst,
            tmp_path / "a" / "Games.exe",
            which=lambda c: "/x/7z",
            run=_fake_7z([], games=b"<html>"),
        )
    with pytest.raises(es.ShimError, match="7z"):
        es.extract_games_exe(
            inst, tmp_path / "b" / "Games.exe", which=lambda c: None, run=_fake_7z([])
        )
    with pytest.raises(es.ShimError, match="Games.exe"):
        es.extract_games_exe(
            inst,
            tmp_path / "c" / "Games.exe",
            which=lambda c: "/x/7z",
            run=_fake_7z([], place=False),
        )


def test_ensure_files_prefers_the_cache_then_the_prefix_then_downloads(tmp_path):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    src = tmp_path / "s.c"
    src.write_text("x")
    built: list = []
    kw = dict(source=src, cc="g", which=lambda c: "/x/" + c, run=_fake_cc(built))

    # 1. nothing anywhere: downloads the installer and extracts Games.exe
    dl: list = []

    def download(url, dest):
        dl.append(url)
        Path(dest).write_bytes(b"nsis")

    calls: list = []
    files = es.ensure_files(
        prefix,
        data_dir=tmp_path / "data",
        fetch_json=lambda url: {
            "exe_url": "https://launcher.hg-cdn.com/GryphlineLauncher.exe?tracking=X"
        },
        download=download,
        extract_run=_fake_7z(calls),
        **kw,
    )
    assert dl == ["https://launcher.hg-cdn.com/GryphlineLauncher.exe?tracking=X"]
    assert (
        (files.shim.parent == files.games.parent)
        and files.shim.name == "suspend_start.exe"
        and files.games.name == "Games.exe"
    )
    assert files.shim.is_file() and files.games.read_bytes() == MZ
    assert str(files.shim).startswith(
        str(prefix / "drive_c")
    )  # staged inside the prefix so C:\ paths work

    # 2. second call: nothing is downloaded or extracted again
    es.ensure_files(
        prefix,
        data_dir=tmp_path / "data",
        fetch_json=lambda u: 1 / 0,
        download=lambda u, d: 1 / 0,
        extract_run=lambda *a, **k: 1 / 0,
        **kw,
    )

    # 3. a launcher already installed in the prefix is used without any download
    prefix2 = tmp_path / "pfx2"
    d = prefix2 / "drive_c" / "Program Files" / "GRYPHLINK" / "0.1.25"
    d.mkdir(parents=True)
    (d / "Games.exe").write_bytes(MZ + b"real")
    f2 = es.ensure_files(
        prefix2,
        data_dir=tmp_path / "data2",
        fetch_json=lambda u: 1 / 0,
        download=lambda u, d: 1 / 0,
        extract_run=lambda *a, **k: 1 / 0,
        **kw,
    )
    assert f2.games.read_bytes() == MZ + b"real"


def _obtain(tmp_path, url, download):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    return es._obtain_games(
        prefix,
        tmp_path / "data" / "Games.exe",
        fetch_json=lambda _url: {"exe_url": url},
        download=download,
        which=lambda c: "/x/7z",
        extract_run=_fake_7z([]),
    )


def test_obtain_games_downloads_from_an_allowed_installer_host(tmp_path):
    url = "https://launcher.hg-cdn.com/CNKz/GryphlineLauncher.exe"
    calls: list = []

    def download(got_url, dest):
        calls.append(got_url)
        Path(dest).write_bytes(b"nsis")

    cache = _obtain(tmp_path, url, download)
    assert calls == [url] and cache.read_bytes() == MZ


def test_obtain_games_refuses_a_url_off_the_installer_allowlist(tmp_path):
    calls: list = []

    def download(url, dest):
        calls.append(url)

    with pytest.raises(es.ShimError, match="allowed host"):
        _obtain(tmp_path, "https://evil-hg-cdn.com/GryphlineLauncher.exe", download)
    assert calls == []
    assert not (tmp_path / "data" / "Games.exe").exists()


def test_suspend_start_quote_obeys_windows_rules_and_buffer_capacity(tmp_path):
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("gcc is not available")
    source = (Path(es.__file__).with_name("shim_src") / "suspend_start.c").read_text()
    helper = re.search(
        r"static int append\(.*?\nstatic void quote\(.*?\n\}\n", source, re.DOTALL
    )
    assert helper is not None, "cannot find append()/quote() in suspend_start.c"
    program = r"""
#include <stddef.h>
#include <stdio.h>
#include <wchar.h>

typedef wchar_t WCHAR;

""" + helper.group(0) + r"""
static int failures;

static void check(const char *name, const WCHAR *got, const WCHAR *want)
{
    if (wcscmp(got, want)) {
        fprintf(stderr, "%s: got [%ls] want [%ls]\n", name, got, want);
        failures++;
    }
}

int main(void)
{
    WCHAR buf[128];
    WCHAR escaped[] = { L'"', L'a', L'\\', L'\\', L'\\', L'"', L'b', L'"', 0 };
    WCHAR big[16];
    int i;

    buf[0] = 0;
    quote(buf, 128, L"plain");
    check("plain", buf, L"\"plain\"");
    buf[0] = 0;
    quote(buf, 128, L"a b");
    check("space", buf, L"\"a b\"");
    buf[0] = 0;
    quote(buf, 128, L"say \"hi\"");
    check("quotes", buf, L"\"say \\\"hi\\\"\"");
    buf[0] = 0;
    quote(buf, 128, L"C:\\Games\\foo\\");
    check("trailing", buf, L"\"C:\\Games\\foo\\\\\"");
    buf[0] = 0;
    quote(buf, 128, L"a\\\"b");
    check("before-quote", buf, escaped);

    for (i = 0; i < 16; i++) big[i] = 0xAA;
    big[0] = 0;
    quote(big, 8, L"abcdef");
    check("truncated", big, L"\"abcdef");
    if (big[7] != 0) failures++;
    for (i = 8; i < 16; i++) if (big[i] != 0xAA) failures++;

    return failures ? 1 : 0;
}
"""
    c_file = tmp_path / "quote_test.c"
    exe = tmp_path / "quote_test"
    c_file.write_text(program)
    built = subprocess.run(
        [gcc, "-std=c11", "-Wall", "-Wextra", "-Werror", "-o", str(exe), str(c_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert built.returncode == 0, built.stderr
    ran = subprocess.run([str(exe)], capture_output=True, text=True, check=False)
    assert ran.returncode == 0, ran.stderr


# ------------------------------------------------ #527: errors name the failing step
def test_shim_error_carries_a_step_and_a_hint_in_its_text():
    err = es.ShimError("boom", step="extract", hint="try this")
    assert err.step == "extract" and err.hint == "try this"
    assert str(err) == "boom (step: extract). try this"
    assert str(es.ShimError("plain")) == "plain" and es.ShimError("plain").step == ""


def test_a_missing_compiler_is_the_build_step_with_install_advice(tmp_path):
    src = tmp_path / "s.c"
    src.write_text("x")
    with pytest.raises(es.ShimError) as caught:
        es.build_shim(
            tmp_path / "o.exe", source=src, cc=None, which=lambda c: None, run=_fake_cc([])
        )
    assert caught.value.step == "build" and "install mingw-w64" in str(caught.value).lower()


def test_a_compiler_failure_is_the_build_step(tmp_path):
    src = tmp_path / "s.c"
    src.write_text("x")
    with pytest.raises(es.ShimError) as caught:
        es.build_shim(
            tmp_path / "o.exe", source=src, cc="g", which=lambda c: c, run=_fake_cc([], fail=True)
        )
    assert caught.value.step == "build" and "boom" in str(caught.value)


def test_a_launcher_api_network_failure_is_the_fetch_step_and_mentions_the_network(tmp_path):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)

    def down(_url):
        raise OSError("connection refused")

    with pytest.raises(es.ShimError) as caught:
        es._obtain_games(
            prefix,
            tmp_path / "data" / "Games.exe",
            fetch_json=down,
            download=lambda u, d: None,
            which=lambda c: "/x/7z",
            extract_run=_fake_7z([]),
        )
    text = str(caught.value).lower()
    assert caught.value.step == "fetch" and "connection refused" in text and "network" in text


def test_an_installer_download_failure_is_the_fetch_step(tmp_path):
    def broken(url, dest):
        raise es._dl.DownloadError("HTTP 404")

    with pytest.raises(es.ShimError) as caught:
        _obtain(tmp_path, "https://launcher.hg-cdn.com/x.exe", broken)
    assert caught.value.step == "fetch" and "404" in str(caught.value)
    assert not list((tmp_path / "data").glob("launcher-*.exe"))  # no temp file left behind


def test_an_unexpected_api_answer_is_the_fetch_step_and_says_the_publisher_changed(tmp_path):
    prefix = tmp_path / "pfx"
    (prefix / "drive_c").mkdir(parents=True)
    with pytest.raises(es.ShimError) as caught:
        es._obtain_games(
            prefix,
            tmp_path / "data" / "Games.exe",
            fetch_json=lambda _u: {"nothing": 1},
            download=lambda u, d: None,
            which=lambda c: "/x/7z",
            extract_run=_fake_7z([]),
        )
    assert caught.value.step == "fetch" and "report" in str(caught.value).lower()


def test_missing_7zip_is_the_extract_step_with_install_advice(tmp_path):
    with pytest.raises(es.ShimError) as caught:
        es.extract_games_exe(tmp_path / "i.exe", tmp_path / "o.exe", which=lambda c: None)
    assert caught.value.step == "extract" and "7" in str(caught.value)


def test_an_installer_without_games_exe_is_the_extract_step_and_says_to_report_it(tmp_path):
    installer = tmp_path / "i.exe"
    installer.write_bytes(b"nsis")
    with pytest.raises(es.ShimError) as caught:
        es.extract_games_exe(
            installer, tmp_path / "o.exe", which=lambda c: "/x/7z", run=_fake_7z([], place=False)
        )
    assert caught.value.step == "extract" and "report" in str(caught.value).lower()
