# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistence uses structural kind metadata, independently of backend imports (#1461)."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


def _isolated(code, tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "config.json")],
        env=dict(os.environ), capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("kind", "catalog"),
    [("direct-bd2", True), ("arknights-yostar", True), ("fgo-lepton", True),
     ("wine", False), ("script", False), ("steam", False),
     ("launcher-nte", False), ("launcher-gfl2-haoplay", False),
     ("unknown-kind", False), (None, False), (["direct-bd2"], False)],
)
def test_historical_kind_classification(tmp_path, kind, catalog):
    _isolated(f"""
from nadare.core import backends, location
kind = {kind!r}
assert backends.one_per_game(kind) is {catalog!r}
assert location.catalog_kind(kind) is {catalog!r}
profile = {{'backend': kind, 'location': {{'old': 'record'}}}}
assert (location.build_record(profile, 'sample') is not None) is {catalog!r}
location.refresh_record(profile, 'sample')
assert ('location' in profile) is {catalog!r}
""", tmp_path)


def test_registered_custom_kind_keeps_catalog_storage(tmp_path):
    _isolated("""
import sys
from pathlib import Path
from nadare.core import backends, location
from nadare.core.config import Config
class Custom: pass
backends.register('custom-game', Custom)
assert backends.one_per_game('custom-game')
assert location.catalog_kind('custom-game')
cfg = Config.load(Path(sys.argv[1]))
cfg.profiles['custom'] = {'backend': 'custom-game'}
cfg.save()
assert location.record_is_current(cfg.profiles['custom'])
assert backends.get('custom-game') is Custom
""", tmp_path)


# This hook observes attempted imports too: a swallowed import error must fail the test.
_BLOCK_IMPORTS = """
import importlib.abc
import sys
class NoImplementations(importlib.abc.MetaPathFinder):
    attempts = []
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'nadare.backends' or fullname.startswith('nadare.backends.'):
            self.attempts.append(fullname)
            raise ImportError('implementation imports forbidden during persistence')
blocker = NoImplementations()
sys.meta_path.insert(0, blocker)
"""


def test_old_config_save_refreshes_without_importing_implementations(tmp_path):
    _isolated(_BLOCK_IMPORTS + """
import json
from pathlib import Path
from nadare.core.config import Config
path = Path(sys.argv[1])
kinds = ['direct-bd2', 'arknights-yostar', 'fgo-lepton', 'stove-czn',
         'stellasora-yostar', 'launcher-nikke', 'wine', 'script', 'steam',
         'launcher-nte', 'launcher-gfl2-haoplay', 'unknown-kind']
profiles = {kind: {'backend': kind, 'future_profile_key': {'value': [1, 2]}}
            for kind in kinds}
# Old-format config: no location records, legacy path keys and unknown data.
profiles['direct-bd2']['wineprefix'] = str(path.parent / 'old-prefix')
path.write_text(json.dumps({'profiles': profiles, 'future_top_key': {'keep': True}}))
cfg = Config.load(path)
cfg.save()
assert blocker.attempts == [], blocker.attempts
assert 'nadare.core.backends' not in sys.modules
saved = json.loads(path.read_text())
assert saved['future_top_key'] == {'keep': True}
from nadare.core import location
for kind in kinds:
    profile = saved['profiles'][kind]
    assert profile['future_profile_key'] == {'value': [1, 2]}
    assert ('location' in profile) == (kind in kinds[:6]), kind
    if kind in kinds[:6]:
        assert location.record_is_current(profile), kind
profile = cfg.profiles['direct-bd2']
assert profile['prefix'] == str(path.parent / 'old-prefix')
profile['prefix'] = str(path.parent / 'moved-prefix')
cfg.save()
assert blocker.attempts == []
record = json.loads(path.read_text())['profiles']['direct-bd2']['location']
assert record['prefix'] == str(path.parent / 'moved-prefix')
assert record['from']['prefix'] == record['prefix']
""", tmp_path)


@pytest.mark.parametrize(
    ("kind", "error"),
    [("fgo-lepton", "ModuleNotFoundError"), ("arknights-yostar", "RuntimeError")],
)
def test_unavailable_implementation_preserves_record_and_operation_errors(tmp_path, kind, error):
    _isolated(f"""
import importlib
import json
import sys
from pathlib import Path
from nadare.core import backends, location
from nadare.core.config import Config
kind = {kind!r}
cfg = Config.load(Path(sys.argv[1]))
cfg.profiles['sample'] = {{'backend': kind}}
cfg.save()
original = cfg.profiles['sample']['location'].copy()
backends._REGISTRY.pop(kind, None)
module = backends._BUILTINS[kind].partition(':')[0]
real_import = importlib.import_module
attempts = []
def broken(name, *args, **kwargs):
    if name == module:
        attempts.append(name)
        raise {error}('synthetic implementation failure')
    return real_import(name, *args, **kwargs)
importlib.import_module = broken
cfg.save()
assert cfg.profiles['sample']['location'] == original
assert json.loads(cfg.path.read_text())['profiles']['sample']['location'] == original
assert attempts == [], attempts
assert not backends.is_available(kind)
assert kind in backends.unavailable()
try:
    backends.get(kind)
except backends.BackendError as exc:
    assert '{error}: synthetic implementation failure' in str(exc)
else:
    raise AssertionError('operation must still report unavailable backend')
""", tmp_path)


def test_a_retired_kind_never_has_a_storage_class_even_if_listed(monkeypatch):
    """Retired kinds keep their historical meaning (no layout), even if listed by mistake."""
    from nadare.core import directkinds

    retired = sorted(directkinds.RETIRED_KINDS)[0]
    monkeypatch.setitem(directkinds._STORAGE_CLASSES, retired, "catalog")
    assert directkinds.storage_class(retired) is None
    assert directkinds.one_per_game(retired) is False
