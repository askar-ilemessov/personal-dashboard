"""The stats engine: pure, deterministic functions over plain numbers.

No database, no LLM. Everything the app says about averages, trends,
correlations and anomalies is computed here; language models only word
the results. A value of None always means "not logged" and is left out,
never treated as zero.
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import fmean, median

MIN_PAIRS = 14  # fewer paired days than this and a correlation is not reported


def clean(values) -> list[float]:
    return [float(v) for v in values if v is not None]


def mean(values) -> float | None:
    xs = clean(values)
    return fmean(xs) if xs else None


def stdev(values) -> float | None:
    xs = clean(values)
    if len(xs) < 2:
        return None
    m = fmean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def change(current, previous) -> float | None:
    """Relative change, e.g. 0.13 for +13%. None when it can't be computed."""
    if current is None or previous is None or float(previous) == 0:
        return None
    return (float(current) - float(previous)) / abs(float(previous))


# --- correlation -------------------------------------------------------------


def paired(xs: dict, ys: dict, lag: int = 0) -> list[tuple[float, float]]:
    """(x on day D, y on day D + lag) for every day where both are logged."""
    out = []
    for day, x in xs.items():
        y = ys.get(day + timedelta(days=lag))
        if x is not None and y is not None:
            out.append((float(x), float(y)))
    return out


def pearson(pairs) -> float | None:
    n = len(pairs)
    if n < 2:
        return None
    mx, my = fmean(p[0] for p in pairs), fmean(p[1] for p in pairs)
    sxx = sum((x - mx) ** 2 for x, _ in pairs)
    syy = sum((y - my) ** 2 for _, y in pairs)
    if sxx == 0 or syy == 0:
        return None  # one side never varies: no relationship to measure
    return sum((x - mx) * (y - my) for x, y in pairs) / math.sqrt(sxx * syy)


def _ranks(values: list[float]) -> list[float]:
    """Average ranks, so ties share a rank."""
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return ranks


def spearman(pairs) -> float | None:
    """Rank correlation: robust to outliers and to non-linear but monotonic
    relationships, which suits small, messy personal data."""
    if len(pairs) < 2:
        return None
    rx, ry = _ranks([p[0] for p in pairs]), _ranks([p[1] for p in pairs])
    return pearson(list(zip(rx, ry, strict=True)))


def p_value(r: float, n: int) -> float | None:
    """Two-sided p-value for a correlation, by the Fisher z approximation."""
    if n < 4 or abs(r) >= 1:
        return 0.0 if n >= 4 else None
    z = math.atanh(r) * math.sqrt(n - 3)
    return math.erfc(abs(z) / math.sqrt(2))


@dataclass(frozen=True)
class Correlation:
    rho: float
    n: int
    p: float

    @property
    def strength(self) -> str:
        a = abs(self.rho)
        return "strong" if a >= 0.5 else "moderate" if a >= 0.3 else "weak" if a >= 0.15 else "none"

    @property
    def direction(self) -> str:
        return "positive" if self.rho > 0 else "negative"

    @property
    def reliable(self) -> bool:
        """Worth mentioning: not tiny, and unlikely to be chance."""
        return self.strength in ("moderate", "strong") and self.p < 0.05


def correlate(xs: dict, ys: dict, lag: int = 0, min_pairs: int = MIN_PAIRS) -> Correlation | None:
    pairs = paired(xs, ys, lag)
    if len(pairs) < min_pairs:
        return None
    rho = spearman(pairs)
    if rho is None:
        return None
    return Correlation(rho=rho, n=len(pairs), p=p_value(rho, len(pairs)))


# --- trend ---------------------------------------------------------------------


def slope(series: dict) -> float | None:
    """Least-squares change per day across the logged days."""
    points = [((day - min(series)).days, float(v)) for day, v in series.items() if v is not None]
    if len(points) < 3:
        return None
    mx, my = fmean(p[0] for p in points), fmean(p[1] for p in points)
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in points) / sxx


# --- anomalies -----------------------------------------------------------------


def robust_z(value: float, baseline) -> float | None:
    """How unusual `value` is against a baseline, using median and MAD so
    that earlier outliers don't hide new ones."""
    xs = clean(baseline)
    if len(xs) < 7:
        return None
    med = median(xs)
    mad = median(abs(x - med) for x in xs)
    if mad == 0:
        spread = stdev(xs)
        if not spread:
            return None
        return (value - med) / spread
    return 0.6745 * (value - med) / mad


def outliers(series: dict, since: date, threshold: float = 3.5, high_only: bool = True) -> list[tuple[date, float, float]]:
    """Days on or after `since` whose value is unusual against all the other
    days in the series. Returns (day, value, z), most unusual first."""
    found = []
    for day, v in series.items():
        if v is None or day < since:
            continue
        z = robust_z(float(v), [x for d, x in series.items() if d != day])
        if z is not None and (z >= threshold or (not high_only and z <= -threshold)):
            found.append((day, float(v), z))
    return sorted(found, key=lambda f: -abs(f[2]))


def runs(series: dict, predicate, min_length: int) -> list[tuple[date, date, int]]:
    """Stretches of consecutive logged days that all satisfy `predicate`.
    An unlogged day ends a run. Returns (first day, last day, length)."""
    out, run = [], []

    def close():
        if len(run) >= min_length:
            out.append((run[0], run[-1], len(run)))

    for day in sorted(series):
        v = series[day]
        hit = v is not None and predicate(float(v))
        if hit and run and day - run[-1] == timedelta(days=1):
            run.append(day)
        else:
            close()
            run = [day] if hit else []
    close()
    return out
