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
    """Classify ``value`` against a band. Either bound may be ``None`` (open).

    With both bounds set, the hysteresis margin is a fraction of the band width.
    With only one bound, there is no width to scale from, so the margin is a
    fraction of that bound's magnitude instead.
    """
    if low is not None and high is not None and high <= low:
        raise ValueError("high must be greater than low")

    if low is not None and high is not None:
        margin_low = margin_high = hysteresis_frac * (high - low)
    else:
        margin_low = hysteresis_frac * abs(low) if low is not None else 0.0
        margin_high = hysteresis_frac * abs(high) if high is not None else 0.0

    # Hysteresis only makes CLEARING harder. It must never suppress a fresh
    # alarm: a value that crosses from below-low straight to above-high has not
    # "recovered", and an early return of OK here would report it in spec.
    if previous == LOW and low is not None and value < low + margin_low:
        return LOW
    if previous == HIGH and high is not None and value > high - margin_high:
        return HIGH

    if low is not None and value < low:
        return LOW
    if high is not None and value > high:
        return HIGH
    return OK

def robust_sigma(values) -> float:
    if len(values) < 2:
        return 0.0
    centre = median(values)
    return 1.4826 * median([abs(v - centre) for v in values])


def _robust_line(pts):
    """OLS line through (x, y) points, refined by a few rounds of MAD-based
    outlier trimming so a spike inside the window can't tilt it."""
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    slope = 0.0
    for _ in range(3):
        n = len(xs)
        if n < 3:
            break
        xb = sum(xs) / n
        yb = sum(ys) / n
        sxx = sum((x - xb) ** 2 for x in xs)
        sxy = sum((x - xb) * (y - yb) for x, y in zip(xs, ys))
        slope = sxy / sxx if sxx else 0.0
        intercept = yb - slope * xb
        res = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
        mad = median(abs(r) for r in res)
        if mad == 0:
            break
        keep = [i for i, r in enumerate(res) if abs(r) <= 3.5 * mad]
        if len(keep) == n or len(keep) < max(3, n // 2):
            break
        xs = [xs[i] for i in keep]
        ys = [ys[i] for i in keep]
    intercept = median(y - slope * x for x, y in zip(xs, ys))
    return slope, intercept


def local_trend(values, window: int, gap: int = 3) -> list[float]:
    """Baseline for anomaly detection: a robust straight line fitted to the
    window *ending ``gap`` samples before* each point, then extrapolated to it.

    Two design choices matter:

    * A line, not a trailing median. A steady drift then leaves a flat residual
      instead of one that creeps toward the threshold and trips on every sample
      (the trailing median lagged the trend by half a window).
    * The ``gap`` keeps the point under test — and the couple before it — out of
      its own baseline, so a real excursion can't fit itself and disappear, and
      the excursion's echo does not linger in the fit for long afterwards.
    """
    out = []
    n = len(values)
    for i in range(n):
        hi = i - gap
        lo = hi - window + 1
        if lo < 0:                              # not enough clean history yet
            out.append(median(values[max(0, i - window + 1) : i + 1]))
            continue
        pts = list(enumerate(values[lo : hi + 1]))     # local x = 0 .. window-1
        slope, intercept = _robust_line(pts)
        out.append(intercept + slope * (i - lo))       # extrapolate to i
    return out


def anomaly_flags(values, k=4.0, window=21, gap=3) -> list[bool]:
    """Flag points that sit implausibly far from their local trend.

    Robust local linear detrend, then MAD scatter on the residuals: a drift no
    longer biases the residual, and one bad point can neither tilt the line nor
    inflate the threshold that judges it.
    """
    flags = [False] * len(values)
    if len(values) < window:
        return flags                  # not enough history to judge

    baseline = local_trend(values, window, gap)
    residuals = [v - b for v, b in zip(values, baseline)]

    # Scatter from the full-window fits only — the warm-up region falls back to a
    # plain median and would distort the estimate.
    settled = window + gap - 1
    if len(residuals) <= settled:
        return flags
    sigma = robust_sigma(residuals[settled:])
    if sigma <= 0:
        return flags                  # constant or perfectly linear: no scatter

    for i in range(settled, len(values)):
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