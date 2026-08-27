import math
from statistics import median
from typing import Sequence


def ewma_step(previous, value, dt, tau) -> float:
    """Exponential moving average of a single new value.

    previous: previous smoothed value
    value: new raw value
    dt: time since previous value, in seconds
    tau: smoothing time constant, in seconds

    Returns the new smoothed value.
    """
    if dt <= 0.0:
        return previous
    if tau <= 0.0:
        return value
    alpha = 1.0 - math.exp(-dt / tau)
    return (1.0 - alpha) * previous + alpha * value

def ewma_series(times, values, tau: float) -> list[float]:
    if len(times) != len(values):
        raise ValueError("times and values must be the same length")
    if not values:
        return []

    out = [float(values[0])]
    for i in range(1, len(values)):
        out.append(ewma_step(out[-1], values[i], times[i] - times[i - 1], tau))
    return out


OK, LOW, HIGH = "ok", "low", "high"


def range_check(value, low, high, previous=OK, hysteresis_frac=0.1) -> str:
    if high <= low:
        raise ValueError("high must be greater than low")

    margin = hysteresis_frac * (high - low)

    # Hysteresis only makes CLEARING harder. It must never suppress a fresh
    # alarm: a value that crosses from below-low straight to above-high has not
    # "recovered", and an early return of OK here would report it in spec.
    if previous == LOW and value < low + margin:
        return LOW
    if previous == HIGH and value > high - margin:
        return HIGH

    if value < low:
        return LOW
    if value > high:
        return HIGH
    return OK

def robust_sigma(values) -> float:
    if len(values) < 2:
        return 0.0
    centre = median(values)
    return 1.4826 * median([abs(v - centre) for v in values])


def rolling_median(values, window: int) -> list[float]:
    """Median of each trailing window.

    Used as the anomaly baseline instead of the EWMA. An EWMA chases the very
    spike it is meant to expose, which makes the residual small at the spike and
    large for the next twenty samples as it recovers.
    """
    out = []
    for i in range(len(values)):
        start = max(0, i - window + 1)
        out.append(median(values[start : i + 1]))
    return out


def anomaly_flags(values, k=4.0, window=21) -> list[bool]:
    """Flag points that sit implausibly far from their local median.

    Median baseline plus MAD scatter: both halves are robust, so one bad point
    can neither move the baseline nor inflate the threshold that judges it.
    """
    flags = [False] * len(values)
    if len(values) < window:
        return flags                  # not enough history to judge

    baseline = rolling_median(values, window)
    residuals = [v - b for v, b in zip(values, baseline)]

    sigma = robust_sigma(residuals)
    if sigma <= 0:
        return flags                  # constant or perfectly linear: no scatter

    for i in range(window - 1, len(values)):   # skip the warm-up
        flags[i] = abs(residuals[i]) > k * sigma
    return flags

def rate_of_change(times: Sequence[float], values: Sequence[float], per_seconds: float = 1.0) -> list[float]:
    """Compute the rate of change (derivative) of a series of values.

    times: list of timestamps in seconds
    values: list of corresponding values
    per_seconds: factor to scale the rate of change (e.g., 60.0 for minutes)

    Returns a list of rates of change, same length as input.
    The first value is set to 0.0 since there is no previous point to compare.
    """
    if len(times) != len(values):
        raise ValueError("times and values must be the same length")
    if not values:
        return []

    rates = [0.0]  # First value has no previous point
    for i in range(1, len(values)):
        dt = times[i] - times[i - 1]
        if dt <= 0:
            rates.append(0.0)  # Avoid division by zero or negative time
        else:
            rate = (values[i] - values[i - 1]) / dt
            rates.append(rate * per_seconds)
    return rates