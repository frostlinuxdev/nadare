# SPDX-License-Identifier: GPL-3.0-or-later
"""Small helpers shared by the backend tests (not a test module).

``tests/conftest.py`` is shared, so this suite keeps its own fixtures out of it
and imports plain helpers instead.
"""

from __future__ import annotations

from pathlib import Path

from nadare.core.backends import BackendContext
from nadare.core.config import Config


def make_context(
    tmp_path: Path, kind: str, profile: dict | None = None, name: str = "test"
) -> BackendContext:
    """Build an isolated :class:`BackendContext` for ``kind``.

    The profile is written into a throw-away config under ``tmp_path``; the
    autouse ``_isolated_environment`` fixture points all XDG dirs at ``tmp_path``
    as well, so ``ctx.data_dir()`` never escapes the test tree.
    """
    values = dict(profile or {})
    values.setdefault("backend", kind)
    raw = {"profiles": {name: values}}
    cfg = Config(path=tmp_path / f"{name}-config.json", data=Config._normalise(raw))
    return BackendContext(name=name, profile=cfg.profile(name), config=cfg)


def write_exe(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


def make_jwt(sub: str = "P12345", exp: int | None = None) -> str:
    """An unsigned, JWT-shaped token for login tests."""
    import base64
    import json
    import time

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    exp = int(time.time()) + 86400 if exp is None else exp
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64(json.dumps({"sub": sub, "exp": exp}).encode())
    return f"{header}.{payload}.{b64(b'signature-bytes')}"


def x64_dll(extra: bytes = b"") -> bytes:
    """The smallest file the VC++ runtime check accepts as an x64 DLL."""
    head = bytearray(b"MZ" + b"\0" * 0x3E)
    head[0x3C:0x40] = (0x40).to_bytes(4, "little")
    return bytes(head) + b"PE\0\0" + (0x8664).to_bytes(2, "little") + b"\0" * 20 + extra


def fake_launcher_backend():
    """A minimal publisher-launcher game, for tests of the shared launcher code.

    NIKKE is the only real launcher kind left (the NTE and GFL2-Haoplay launcher kinds were
    retired, 2026-11-10); this stands in for "any other launcher game".
    """
    from nadare.backends.launcher import LauncherBackend

    class FakeLauncherBackend(LauncherBackend):
        kind = "launcher-fake"
        game_tag = "fake-launcher"
        display_name = "Fake launcher game"
        install_subdir = Path("drive_c/Program Files/Fake")
        launcher_names = ("FakeLauncher.exe",)
        direct_exe = "FakeLauncher.exe"
        default_installer_url = "https://dl.fake-publisher.example/setup.exe"
        trusted_hosts = ("fake-publisher.example",)

    return FakeLauncherBackend
