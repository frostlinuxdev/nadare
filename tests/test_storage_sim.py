# SPDX-License-Identifier: GPL-3.0-or-later
"""The storage simulator in CI: a fixed set of seeds must pass every invariant (#1372).

The big run is ``python tests/storage_sim.py --runs 1000``; here 40 seeds keep CI quick.
"""

from __future__ import annotations

import pytest
from storage_sim import execute


@pytest.mark.parametrize("seed", range(40))
def test_a_random_world_survives_random_actions(seed):
    run = execute(seed)
    assert not run.violations, "\n".join(
        [f"{v.category}: {v.detail}" for v in run.violations] + run.log
    )
