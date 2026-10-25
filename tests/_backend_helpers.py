# SPDX-License-Identifier: GPL-3.0-or-later
"""Small helpers shared by the backend tests (not a test module).

``tests/conftest.py`` is owned by another worker, so this suite keeps its own
fixtures out of it and imports plain helpers instead.
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
