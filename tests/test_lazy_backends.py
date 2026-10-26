"""Backend discovery should not import unrelated implementations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _fresh_python(code: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return json.loads(result.stdout)


def test_get_imports_only_the_requested_backend():
    result = _fresh_python(
        """
import json
import sys
from nadare.core import backends
class Config:
    def profile(self, name):
        return {"backend": "wine"}
backends.create("test", Config())
print(json.dumps(sorted(
    name for name in sys.modules
    if name.startswith("nadare.backends.")
)))
"""
    )

    assert "nadare.backends.wine" in result
    assert set(result) == {"nadare.backends.wine"}


def test_unavailable_still_reports_a_backend_with_a_missing_module():
    result = _fresh_python(
        """
import json
import sys
from nadare.core import backends
sys.modules["nadare.backends.fgo"] = None
print(json.dumps(backends.unavailable()))
"""
    )

    assert "fgo-lepton" in result
