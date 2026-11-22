# SPDX-License-Identifier: GPL-3.0-or-later
"""The GUI asks for the account notice before Repair and Rebuild prefix too (#1567).

They change the game files of an unofficial-launcher game just as Install and Update do,
and the CLI already asked; the GUI is the reference and now matches.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from nadare.gui.main_window_parts import confirm


@pytest.mark.parametrize("operation", ["repair", "remake_prefix"])
def test_the_gui_asks_for_the_account_notice_before(operation):
    asked = []
    window = SimpleNamespace(
        _needs_notice=lambda profile: True,
        _confirm_notice=lambda profile: asked.append(profile) or False,
    )
    assert confirm.NOTICE_OPERATIONS >= {operation}
    method = next(
        cls._confirm_action
        for cls in vars(confirm).values()
        if isinstance(cls, type) and "_confirm_action" in vars(cls)
    )
    assert method(window, "g", operation) is False
    assert asked == ["g"]
