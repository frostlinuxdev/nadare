# SPDX-License-Identifier: GPL-3.0-or-later
"""WebView2 download/staging paths are isolated between game prefixes."""

from pathlib import Path

from _backend_helpers import make_context

from nadare.backends.czn.backend import CznBackend
from nadare.core import graphics


def test_two_prefixes_get_distinct_webview2_work_files(tmp_path, monkeypatch):
    cache = tmp_path / "shared-cache"
    downloads: list[Path] = []
    runs: list[Path] = []

    monkeypatch.setattr(graphics, "has_display", lambda: True)
    for name in ("profile-a", "profile-b"):
        state = {"installed": False}
        prefix = tmp_path / name / "prefix"
        backend = CznBackend(
            make_context(
                tmp_path / name,
                "stove-czn",
                {"prefix": str(prefix)},
                name,
            )
        )
        monkeypatch.setattr(backend, "_cache_dir", lambda: cache)
        monkeypatch.setattr(
            backend, "_webview2_installed", lambda state=state: state["installed"]
        )
        monkeypatch.setattr(backend, "_webview2_pin", lambda: "0" * 64)
        monkeypatch.setattr(backend, "_webview2_overridden", lambda: True)
        monkeypatch.setattr(backend, "_verify_webview2", lambda _path: None)

        def fake_download(_url, part: Path, installer: Path) -> None:
            downloads.append(part)
            part.write_bytes(b"mock installer")
            part.replace(installer)

        def fake_run(argv, state=state, runs=runs):
            runs.append(Path(argv[0]))
            state["installed"] = True
            return 0

        monkeypatch.setattr(backend, "_download_webview2", fake_download)
        monkeypatch.setattr(backend, "_run_in_prefix", fake_run)
        backend._ensure_webview2()
        (cache / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe").unlink()

    assert downloads[0] != downloads[1]
    assert runs[0] != runs[1]
