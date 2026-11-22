# SPDX-License-Identifier: GPL-3.0-or-later
"""#1299: a default that old profiles rely on must not change silently.

Old configs do not store a value that equals the default, so changing a default changes what
every existing profile does (b22's Sandbox default, WuWa's display default 2026-11-12). This is a
tripwire: when one of these values changes, either

* bump ``config.SCHEMA`` and add a ``MIGRATIONS`` step that writes the old value into existing
  profiles, with an upgrade test (old data as the old release wrote it, opened by the new code), or
* the change is meant to reach existing profiles: say so in the release notes and refresh the
  snapshot with ``NADARE_REFRESH_DEFAULTS=1 pytest tests/test_stored_defaults_1299.py``.

New keys and new backends are fine: older code has nothing stored under them.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path

from nadare.core import backends, config, gamesandbox

SNAPSHOT = Path(__file__).parent / "fixtures" / "stored_defaults.json"


def _source_value(module, name: str) -> object:
    """``name`` as the module's source assigns it: conftest turns the Sandbox default off for
    every test, so the attribute would only show the test value."""
    tree = ast.parse(inspect.getsource(module))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{module.__name__}.{name} is not assigned at module level")


def current() -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in config.PROFILE_DEFAULTS.items():
        values[f"profile.{key}"] = value
    for key, value in config.TOP_DEFAULTS.items():
        if key != "schema":
            values[f"top.{key}"] = value
    values["sandbox.DEFAULT_ON"] = _source_value(gamesandbox, "DEFAULT_ON")
    backends.load_builtins()
    for kind in backends.kinds():
        if not backends.is_available(kind):
            continue
        cls = backends.get(kind)
        values[f"backend.{kind}.default_display"] = getattr(cls, "default_display", None)
        values[f"backend.{kind}.sandbox_default"] = backends.sandbox_default(kind)
    return json.loads(json.dumps(values, sort_keys=True, default=str))


def test_stored_defaults_did_not_change_silently():
    now = current()
    if os.environ.get("NADARE_REFRESH_DEFAULTS") == "1":
        SNAPSHOT.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
    recorded = json.loads(SNAPSHOT.read_text())
    changed = {
        key: (old, now.get(key, "<removed>"))
        for key, old in recorded.items()
        if now.get(key, "<removed>") != old
    }
    assert not changed, (
        "these defaults changed, which changes every existing profile that never stored them: "
        f"{changed}. Add a config migration (bump SCHEMA) with an upgrade test, or refresh the "
        "snapshot on purpose (see this file's docstring)."
    )


def test_the_snapshot_covers_the_display_and_sandbox_defaults():
    recorded = json.loads(SNAPSHOT.read_text())
    assert "sandbox.DEFAULT_ON" in recorded
    assert any(key.endswith(".default_display") for key in recorded)
    assert any(key.startswith("profile.") for key in recorded)
