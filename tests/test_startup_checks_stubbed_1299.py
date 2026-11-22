# SPDX-License-Identifier: GPL-3.0-or-later
"""A window's 3 s start-up checks (#1299) start nothing in unit tests.

CI run 2403 (2026-11-21): a window left over from an earlier GUI test fired its 3 s timer while
``test_flatpak_reach_1299`` had patched ``sandbox.in_flatpak`` to True; the real Flatpak folder
check then found that window's games out of reach and opened a real message box, which aborted
the xdist worker. Tests of the checks call ``CleanupMixin`` (or ``_real_start_*``) directly.
"""

from __future__ import annotations

import pytest

CHECKS = ("_start_sandbox_broad_check", "_start_flatpak_reach_check", "_start_sandbox_trust_check")


@pytest.mark.parametrize("name", CHECKS)
def test_a_window_s_own_timer_starts_no_startup_check_in_unit_tests(name):
    pytest.importorskip("PyQt6")
    from nadare.gui.main_window import MainWindow
    from nadare.gui.main_window_parts.cleanup import CleanupMixin

    assert getattr(MainWindow, name)(object()) is None  # touches nothing
    assert getattr(MainWindow, "_real" + name) is getattr(CleanupMixin, name)
