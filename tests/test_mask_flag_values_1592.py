# SPDX-License-Identifier: GPL-3.0-or-later
"""#1592 item 4: a secret passed as a separate argument (``--password hunter2``) is masked.

``key=value`` was already masked; the space-separated form of a command line was not, so the
support report kept it from ``launch_args``.
"""

from __future__ import annotations

import pytest

from nadare.core import events, report

SECRET = "hunter2-SYNTH"


@pytest.mark.parametrize(
    "text",
    [
        f"--password {SECRET}",
        f"-password {SECRET}",
        f"--token {SECRET}",
        f"--auth-token {SECRET}",
        f"--api-key {SECRET}",
        f"-AUTH_PASSWORD {SECRET}",
        f"-login someone -pw {SECRET}",
        f"--passwd {SECRET} -windowed",
        f'--password "{SECRET} with space"',
        f"--password='{SECRET}'",
        f"/password {SECRET}",
    ],
)
def test_a_secret_flag_value_is_masked(text):
    for fn in (events.mask_known, events.mask):
        out = fn(text)
        assert SECRET not in out, (fn.__name__, out)
        assert "<redacted>" in out


@pytest.mark.parametrize(
    "text",
    [
        "-windowed -screen-width 1920",
        "--passthrough -fullscreen",
        "token expired, signing in again",
        "-password",  # a flag with nothing after it
        "--token --no-splash",  # the next item is another flag, not a value
        "-skip-intro -nosplash",
    ],
)
def test_ordinary_arguments_stay_readable(text):
    assert events.mask_known(text) == text


def test_the_flag_name_and_following_arguments_survive():
    out = events.mask_known(f"-windowed --password {SECRET} -nosplash")
    assert out == "-windowed --password <redacted> -nosplash"


def test_the_support_report_masks_a_space_separated_secret_in_launch_args():
    cleaned = report._safe_setting("launch_args", f"-nosplash --token {SECRET}")
    assert SECRET not in cleaned
    assert cleaned.startswith("-nosplash --token ")
    nested = report._safe_setting("installer_args", ["/S", "/password", SECRET])
    # A list keeps each argument separate: the value after a secret flag is masked too.
    assert SECRET not in str(nested)
