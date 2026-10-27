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
