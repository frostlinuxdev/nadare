# SPDX-License-Identifier: GPL-3.0-or-later
"""An empty version_info is not a client record: retry without need_launcher."""

from __future__ import annotations

import json

from nadare.core.sources import nikke as source

CLIENT = {
    "game_id": "16601",
    "branch_id": "1",
    "cos_repo_files": "[]",
    "cos_access_info": "[]",
}


def test_an_empty_client_record_still_retries_without_need_launcher():
    seen = []

    def post(url, body, headers):
        need_launcher = json.loads(body)["need_launcher"]
        seen.append(need_launcher)
        version_info = CLIENT if need_launcher == 0 else {}
        return json.dumps(
            {"result": {"error_code": 0}, "version_info": version_info}
        ).encode()

    info = source.client_info(post=post)
    assert info["game_id"] == "16601"
    assert seen == [1, 0]
