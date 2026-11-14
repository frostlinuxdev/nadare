# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Nadare contributors
"""Helpers and constants shared by the parts of nadare.gui.main_window.

Split out of :mod:`nadare.gui.main_window`; the code is unchanged.
"""

from __future__ import annotations

import logging

log = logging.getLogger("nadare.gui.main_window")

#: A fresh install larger than this asks for confirmation first.
DOWNLOAD_CONFIRM_BYTES = 1 << 30
