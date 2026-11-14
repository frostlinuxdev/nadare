# SPDX-License-Identifier: GPL-3.0-or-later
"""The test host's AppArmor userns setting must not reach the suite.

GitHub's runners are Ubuntu 24.04 (``kernel.apparmor_restrict_unprivileged_userns=1``):
before this guard, 51 launch and doctor tests failed there with "Ubuntu blocks the Steam
Runtime..." while passing on Debian runners. Tests of the Ubuntu path patch it themselves.
"""

from __future__ import annotations

from nadare.core import userns


def test_the_suite_sees_an_unrestricted_host_by_default(request):
    assert "_host_userns_unrestricted" in request.fixturenames
    assert userns.restricted() is False
    assert userns.blocks_runtime() is False
