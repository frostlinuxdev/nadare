# SPDX-License-Identifier: GPL-3.0-or-later
"""ETA estimation from a progress fraction: smoothed, quiet until it has enough samples."""

from __future__ import annotations

import pytest

from nadare.core.eta import EtaEstimator, format_eta


def feed(est: EtaEstimator, points):
    out = None
    for t, p in points:
        out = est.update(p, t)
    return out


def test_no_estimate_until_enough_samples_and_time():
    est = EtaEstimator()
    assert est.update(0.01, 0.0) is None
    assert est.update(0.02, 1.0) is None
    assert est.update(0.03, 2.0) is None


def test_steady_progress_gives_the_right_remaining_time():
    est = EtaEstimator()
    pts = [(t, t / 100) for t in range(0, 31)]  # 1 % per second
    eta = feed(est, pts)
    assert eta is not None and abs(eta - 70) < 3


def test_a_single_stall_does_not_blow_up_the_estimate():
    est = EtaEstimator()
    pts = [(t, t / 100) for t in range(0, 21)]
    feed(est, pts)
    eta = feed(est, [(21, 0.20), (22, 0.21)])  # one stalled second
    assert eta is not None and eta < 200


def test_progress_that_restarts_resets_the_estimator():
    est = EtaEstimator()
    feed(est, [(t, 0.5 + t / 200) for t in range(0, 30)])
    assert est.update(0.0, 31.0) is None  # a new phase begins at zero


def test_a_long_gap_resets_the_estimator():
    est = EtaEstimator()
    feed(est, [(t, t / 100) for t in range(0, 20)])
    assert est.update(0.30, 500.0) is None


def test_no_estimate_when_nothing_moves():
    est = EtaEstimator()
    assert feed(est, [(t, 0.10) for t in range(0, 40)]) is None


def test_finished_progress_has_no_estimate():
    est = EtaEstimator()
    assert feed(est, [(t, min(1.0, t / 20)) for t in range(0, 30)]) is None


def test_format_eta_wording():
    assert format_eta(20) == "less than a minute left"
    assert format_eta(60 * 5 + 10) == "about 5 min left"
    assert format_eta(3600 + 20 * 60) == "about 1 h 20 min left"
    assert format_eta(3 * 3600) == "about 3 h left"
    assert format_eta(None) is None
    assert format_eta(60 * 3600) is None  # an estimate that long is noise


def test_a_burst_of_skipped_files_at_the_start_does_not_make_the_estimate_far_too_short():
    """A resumed install jumps over the files already on disk, then downloads at a steady rate."""
    est = EtaEstimator()
    est.update(0.0, 0.0)
    est.update(0.30, 1.0)  # 30 % "done" in one second: files that were already there
    out = None
    seen = []
    for t in range(2, 41):  # then a steady 1 % per second
        out = est.update(0.30 + (t - 1) / 100, float(t))
        if out is not None:
            seen.append((t, out, 100 * (1 - (0.30 + (t - 1) / 100))))
    assert seen, "an estimate should appear once there is enough steady data"
    for t, eta, truth in seen:
        assert abs(eta - truth) < 0.25 * truth + 5, (t, eta, truth)


def test_a_transfer_that_slows_down_is_followed_within_the_window():
    est = EtaEstimator()
    feed(est, [(t, t / 100) for t in range(0, 31)])  # 1 % per second
    eta = feed(est, [(31 + i, 0.30 + (i + 1) / 1000) for i in range(0, 40)])  # now 0.1 % per second
    remaining = 1 - 0.30 - 40 / 1000
    assert eta is not None and 0.6 * (remaining / 0.001) < eta < 1.4 * (remaining / 0.001)


# ---- download speed ----------------------------------------------------------------
from nadare.core.eta import SpeedMeter, format_speed  # noqa: E402

MIB = 1024**2


def test_speed_is_silent_until_it_has_two_samples():
    meter = SpeedMeter()
    assert meter.update(0, 0.0) is None


def test_speed_is_bytes_over_the_recent_window():
    meter = SpeedMeter()
    meter.update(0, 0.0)
    assert meter.update(100 * MIB, 1.0) == pytest.approx(100 * MIB)
    meter.update(200 * MIB, 2.0)
    assert meter.update(300 * MIB, 3.0) == pytest.approx(100 * MIB)


def test_an_old_burst_leaves_the_window():
    meter = SpeedMeter()
    meter.update(0, 0.0)
    meter.update(1000 * MIB, 1.0)  # a resume skipping files: a jump, then quiet
    for second in range(2, 30):
        meter.update(1000 * MIB + (second - 1) * 10 * MIB, float(second))
    assert meter.update(1000 * MIB + 29 * 10 * MIB, 30.0) == pytest.approx(10 * MIB, rel=0.05)


def test_a_burst_after_the_view_was_busy_is_not_a_speed_spike():
    """#1133: events queued while the GUI was busy arrive microseconds apart.

    The bytes behind them came in over the whole pause, so the speed is the real rate,
    not tens of MB divided by a few microseconds (the tab showed ~6 GiB/s).
    """
    rate = 70 * MIB
    meter = SpeedMeter()
    t = 0.0
    while t < 10.0:
        meter.update(rate * t, t)
        t = round(t + 0.1, 6)
    # GUI busy for 6 s (switching tabs); the 60 queued events land at once at t = 16.
    shown = []
    for i in range(1, 61):
        shown.append(meter.update(rate * (10.0 + i * 0.1), 16.0 + i * 1e-6))
    assert max(s for s in shown if s is not None) < 2 * rate


def test_samples_microseconds_apart_give_no_speed_yet():
    meter = SpeedMeter()
    meter.update(0, 0.0)
    assert meter.update(10 * MIB, 0.000_01) is None


def test_speed_starts_over_when_the_counter_drops():
    meter = SpeedMeter()
    meter.update(0, 0.0)
    meter.update(500 * MIB, 5.0)
    assert meter.update(10 * MIB, 6.0) is None  # a new phase began at a lower count


def test_speed_goes_quiet_after_a_long_gap():
    meter = SpeedMeter()
    meter.update(0, 0.0)
    meter.update(50 * MIB, 1.0)
    assert meter.update(60 * MIB, 200.0) is None


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, ""),
        (0, ""),
        (-5, ""),
        (512, "512 B/s"),
        (3 * 1024, "3.0 KiB/s"),
        (85 * MIB, "85.0 MiB/s"),
        (1.5 * 1024**3, "1.50 GiB/s"),
    ],
)
def test_format_speed(value, text):
    assert format_speed(value) == text
