# SPDX-License-Identifier: GPL-3.0-or-later
"""ETA estimation from a progress fraction: smoothed, quiet until it has enough samples."""

from __future__ import annotations

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
