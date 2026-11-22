# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""The ``stove-czn`` backend for Chaos Zero Nightmare (ported from czn-linux).

Public surface: :class:`CznBackend` plus the ported ``auth``/``update``/
``runners`` command modules. The PCSDK stub's source is ``stub/pcsdk-go/`` at the repo root.

``auth.py`` is GPL-3.0-only (it derives from yporoc's launcher, which grants no
"or later"); every other file here is GPL-3.0-or-later.
"""

from __future__ import annotations

from .backend import CznBackend

__all__ = ["CznBackend"]
