# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Helpers and constants shared by the parts of nadare.backends.winegame.

Split out of :mod:`nadare.backends.winegame`; the code is unchanged.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ...core import atomicfile

log = logging.getLogger("nadare.backends.winegame")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``path`` atomically through the shared writer (#1558)."""
    atomicfile.write(path, json.dumps(payload), make_parent=True)
