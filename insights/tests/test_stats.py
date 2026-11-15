from datetime import date, timedelta

import pytest

from insights import stats

D = date(2026, 11, 1)


def series(values, start=D):
    return {start + timedelta(days=i): v for i, v in enumerate(values)}


def test_mean_and_stdev_skip_unlogged_days():
    assert stats.mean([2, None, 4]) == 3
    assert stats.mean([None, None]) is None and stats.mean([]) is None
    assert stats.stdev([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(2.138, abs=1e-3)
    assert stats.stdev([5]) is None


def test_change():
    assert stats.change(113, 100) == pytest.approx(0.13)
    assert stats.change(50, 100) == pytest.approx(-0.5)
    assert stats.change(5, 0) is None and stats.change(None, 3) is None and stats.change(3, None) is None


def test_pearson_known_values():
    assert stats.pearson([(1, 2), (2, 4), (3, 6)]) == pytest.approx(1)
    assert stats.pearson([(1, 6), (2, 4), (3, 2)]) == pytest.approx(-1)
    # Anscombe's first quartet: r = 0.816
    xs = [10, 8, 13, 9, 11, 14, 6, 4, 12, 7, 5]
    ys = [8.04, 6.95, 7.58, 8.81, 8.33, 9.96, 7.24, 4.26, 10.84, 4.82, 5.68]
    assert stats.pearson(list(zip(xs, ys, strict=True))) == pytest.approx(0.816, abs=1e-3)
    assert stats.pearson([(1, 5), (2, 5), (3, 5)]) is None  # y never varies
    assert stats.pearson([(1, 1)]) is None


def test_spearman_uses_ranks_and_handles_ties():
    # Monotonic but far from linear: Spearman sees a perfect relationship.
    assert stats.spearman([(1, 1), (2, 10), (3, 100), (4, 1000)]) == pytest.approx(1)
    # Ties share the average of their ranks.
    pairs = [(1, 1), (2, 1), (3, 2), (4, 2), (5, 2)]
    assert stats._ranks([1, 1, 2, 2, 2]) == [1.5, 1.5, 4, 4, 4]
    assert stats.spearman(pairs) == pytest.approx(0.866, abs=1e-3)
    # One wild outlier barely moves it, unlike Pearson.
    base = [(i, i) for i in range(1, 11)]
    assert stats.spearman([*base, (11, -500)]) > stats.pearson([*base, (11, -500)])


def test_paired_respects_gaps_and_lag():
    xs = series([1, None, 3, 4])
    ys = series([10, 20, None, 40, 50])
    assert stats.paired(xs, ys) == [(1, 10), (4, 40)]
    # lag 1: x on day D against y on the following day
    assert stats.paired(xs, ys, lag=1) == [(1, 20), (3, 40), (4, 50)]


def test_p_value():
    assert stats.p_value(0.5, 30) == pytest.approx(0.0043, abs=5e-4)
    assert stats.p_value(0.1, 30) > 0.5
    assert stats.p_value(-0.5, 30) == stats.p_value(0.5, 30)
    assert stats.p_value(1.0, 20) == 0 and stats.p_value(0.9, 3) is None


def test_correlate_needs_enough_days():
    xs = series(range(13))
    assert stats.correlate(xs, xs) is None  # 13 paired days is not enough
    xs = series(range(30))
    ys = series([v * 2 + (v % 3) for v in range(30)])
    c = stats.correlate(xs, ys)
    assert c.n == 30 and c.rho > 0.95 and c.strength == "strong" and c.direction == "positive" and c.reliable
    flat = series([5] * 30)
    assert stats.correlate(xs, flat) is None


def test_weak_or_chance_correlations_are_not_called_reliable():
    assert not stats.Correlation(rho=0.2, n=200, p=0.004).reliable  # real but too small to matter
    assert not stats.Correlation(rho=0.4, n=15, p=0.14).reliable  # sizeable but could be chance
    assert stats.Correlation(rho=-0.4, n=60, p=0.002).reliable
    assert stats.Correlation(rho=-0.4, n=60, p=0.002).direction == "negative"
    assert [stats.Correlation(r, 50, 0).strength for r in (0.1, 0.2, 0.35, 0.7)] == ["none", "weak", "moderate", "strong"]


def test_slope():
    assert stats.slope(series([1, 2, 3, 4])) == pytest.approx(1)
    assert stats.slope(series([10, None, 6, None, 2])) == pytest.approx(-2)  # gaps keep their place in time
    assert stats.slope(series([1, 2])) is None and stats.slope(series([None, None, None])) is None


def test_outliers_use_a_robust_baseline():
    usual = [40, 55, 38, 60, 45, 52, 47, 58, 41, 50, 44, 56, 49, 53]
    spend = series([*usual, 640, 48, 51])
    [(day, value, z)] = stats.outliers(spend, since=D)
    assert day == D + timedelta(days=14) and value == 640 and z > 10
    # An earlier spike does not hide a later one (a mean/stdev baseline would).
    two = series([*usual, 640, 48, 700])
    assert [d for d, _, _ in stats.outliers(two, since=D)] == [D + timedelta(days=16), D + timedelta(days=14)]
    # Only days in the window are reported; lows only on request.
    assert stats.outliers(spend, since=D + timedelta(days=15)) == []
    low = series([*usual, 1])
    assert stats.outliers(low, since=D) == [] and len(stats.outliers(low, since=D, high_only=False)) == 1
    assert stats.outliers(series([5, 6, 900]), since=D) == []  # too little history to judge


def test_runs_of_short_sleep():
    nights = series([450, 300, 310, 290, 440, None, 320, 330, None, 300, 310, 305, 280])
    short = stats.runs(nights, lambda v: v < 360, min_length=3)
    assert short == [(D + timedelta(days=1), D + timedelta(days=3), 3), (D + timedelta(days=9), D + timedelta(days=12), 4)]
    # An unlogged night breaks a run rather than being assumed short.
    assert stats.runs(series([300, None, 300, 300]), lambda v: v < 360, 3) == []
    assert stats.runs(series([400, 410]), lambda v: v < 360, 1) == []
