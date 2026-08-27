import random
import pytest

from app.processing import (
    HIGH, LOW, OK,
    anomaly_flags, ewma_series, ewma_step,
    range_check, rate_of_change, robust_sigma,
)

### Smoothing tests

def test_ewma_of_constant_series_stays_constant():
    times = [0, 1, 2, 3, 4, 5]
    values = [36.5] * 6
    assert ewma_series(times, values, tau=10.0) == pytest.approx([36.5] * 6)


def test_ewma_converges_towards_a_step():
    times = list(range(60))
    values = [0.0] + [10.0] * 59
    out = ewma_series(times, values, tau=5.0)
    assert out[1] < out[5] < out[20]
    assert out[-1] == pytest.approx(10.0, abs=0.01)


def test_tiny_tau_barely_smooths():
    times = [0, 1, 2, 3]
    values = [1.0, 5.0, 2.0, 8.0]
    assert ewma_series(times, values, tau=1e-6) == pytest.approx(values)


def test_huge_tau_barely_moves():
    times = [0, 1, 2, 3]
    values = [1.0, 5.0, 2.0, 8.0]
    assert ewma_series(times, values, tau=1e6)[-1] == pytest.approx(1.0, abs=0.01)


def test_longer_gap_moves_the_average_further():
    """The property that makes this filter time-aware rather than sample-aware."""
    after_1s = ewma_step(previous=0.0, value=10.0, dt=1.0, tau=10.0)
    after_10s = ewma_step(previous=0.0, value=10.0, dt=10.0, tau=10.0)
    assert after_10s > after_1s
    assert after_1s == pytest.approx(0.952, abs=0.001)
    assert after_10s == pytest.approx(6.321, abs=0.001)


def test_ewma_handles_empty_and_single_point():
    assert ewma_series([], [], tau=5.0) == []
    assert ewma_series([0], [7.0], tau=5.0) == [7.0]


def test_ewma_ignores_non_advancing_timestamps():
    assert ewma_step(previous=5.0, value=99.0, dt=0.0, tau=10.0) == 5.0
    assert ewma_step(previous=5.0, value=99.0, dt=-3.0, tau=10.0) == 5.0


def test_mismatched_lengths_raise():
    with pytest.raises(ValueError):
        ewma_series([0, 1], [1.0], tau=5.0)

### Range-check tests

@pytest.mark.parametrize(
    "value,expected",
    [(6.9, LOW), (7.0, OK), (7.5, OK), (8.0, OK), (8.1, HIGH)],
)
def test_range_check_classifies_from_ok(value, expected):
    assert range_check(value, low=7.0, high=8.0, previous=OK) == expected


def test_limits_themselves_are_in_spec():
    assert range_check(7.0, low=7.0, high=8.0) == OK
    assert range_check(8.0, low=7.0, high=8.0) == OK


def test_hysteresis_holds_the_alarm_until_the_value_comes_properly_back():
    # band is 1.0 wide, so the 10% margin is 0.1: clearing LOW needs >= 7.1
    assert range_check(7.05, low=7.0, high=8.0, previous=LOW) == LOW
    assert range_check(7.10, low=7.0, high=8.0, previous=LOW) == OK


def test_hysteresis_works_at_the_top_of_the_band_too():
    assert range_check(7.95, low=7.0, high=8.0, previous=HIGH) == HIGH
    assert range_check(7.90, low=7.0, high=8.0, previous=HIGH) == OK

def test_hysteresis_never_suppresses_a_fresh_alarm_on_the_other_side():
    """A value crossing from below-low straight to above-high is NOT 'recovered'."""
    assert range_check(40.5, low=36.0, high=37.5, previous=LOW) == HIGH
    assert range_check(30.0, low=36.0, high=37.5, previous=HIGH) == LOW


def test_without_hysteresis_a_value_on_the_limit_flaps():
    """Demonstrates the bug that hysteresis exists to prevent."""
    states = [
        range_check(v, low=7.0, high=8.0, previous=OK, hysteresis_frac=0.0)
        for v in (6.99, 7.01, 6.99, 7.01)
    ]
    assert states == [LOW, OK, LOW, OK]


def test_inverted_limits_raise():
    with pytest.raises(ValueError):
        range_check(7.5, low=8.0, high=7.0)

### Anomaly detection tests
def test_pure_noise_produces_no_flags():
    rng = random.Random(42)
    times = list(range(200))
    values = [36.5 + rng.gauss(0, 0.1) for _ in times]
    smoothed = ewma_series(times, values, tau=10.0)
    assert not any(anomaly_flags(values, smoothed))


def test_a_single_spike_is_caught_exactly_once():
    rng = random.Random(42)
    times = list(range(200))
    values = [36.5 + rng.gauss(0, 0.1) for _ in times]
    values[120] += 8 * 0.1              # an eight-sigma excursion
    smoothed = ewma_series(times, values, tau=10.0)

    flags = anomaly_flags(values, smoothed)
    assert flags[120] is True
    assert sum(flags) == 1


def test_a_slow_drift_is_not_an_anomaly():
    times = list(range(200))
    values = [36.5 + 0.01 * t for t in times]   # 0.6 per minute climb
    smoothed = ewma_series(times, values, tau=10.0)
    assert not any(anomaly_flags(values, smoothed))


def test_too_little_history_flags_nothing():
    assert anomaly_flags([1.0, 50.0, 1.0], [1.0, 1.0, 1.0]) == [False] * 3


def test_robust_sigma_resists_an_outlier_where_stdev_does_not():
    from statistics import stdev

    rng = random.Random(7)
    clean = [rng.gauss(0, 1) for _ in range(50)]
    dirty = clean + [500.0]

    assert robust_sigma(dirty) == pytest.approx(robust_sigma(clean), rel=0.05)
    assert stdev(dirty) > 10 * stdev(clean)

### Rate-of-change tests

def test_rate_of_change_on_a_linear_ramp_is_the_slope():
    times = list(range(10))
    values = [2.0 * t for t in times]          # 2 units per second
    out = rate_of_change(times, values, per_seconds=60.0)
    assert out[0] == 0.0
    assert all(r == pytest.approx(120.0) for r in out[1:])


def test_rate_of_change_is_signed():
    assert rate_of_change([0, 10], [10.0, 0.0])[1] == pytest.approx(-60.0)


def test_rate_of_change_survives_a_repeated_timestamp():
    assert rate_of_change([0, 0], [1.0, 5.0])[1] == 0.0