"""Indicators and setups, translated from the stock pack to FX.

The original daily screen looked for a 20-day breakout, a bull flag, a reclaim
of the 20-day average, an oversold RSI and an oversold bounce. FX gets the
same five, plus their short-side mirrors (breakdown, bear flag, rejection,
overbought, overbought bounce). The opening range is the same idea as the
9:30-9:45 range, moved to a Forex session (London by default).
"""

import datetime

# FX moves are smaller than stock moves. The original bull-flag test was a 10%
# run with a 5% five-day range; 1.5% and 0.8% are the same shape on a major.
BULL_FLAG_RUN_PCT = 1.5
BULL_FLAG_RANGE_PCT = 0.8


def rsi(closes, n=14):
    """Wilder's RSI, the same math as the original scanner."""
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / float(n)


def atr(bars, n=14):
    """Wilder's ATR on OHLC bars (oldest first)."""
    if len(bars) < 2:
        return None
    trs = []
    prev = bars[0]["c"]
    for b in bars[1:]:
        trs.append(max(b["h"] - b["l"], abs(b["h"] - prev), abs(b["l"] - prev)))
        prev = b["c"]
    if not trs:
        return None
    n = min(n, len(trs))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def completed_daily_bars(bars, now_utc, ny_today=None):
    """Drop the still-forming daily bar.

    A D1 bar covers 24 hours from its open. If its open plus 24h is in the
    future, the bar is incomplete and is dropped. This works whatever the
    broker's server date happens to be.
    """
    if not bars:
        return []
    return [b for b in bars
            if b["t_utc"] + datetime.timedelta(hours=24) <= now_utc]


def _ny_date(utc):
    from .timeutil import to_ny
    return to_ny(utc).date()


def find_setups(closes):
    """Daily closes (oldest first, >= 21) -> ({long: [...], short: [...]}, numbers)."""
    c = [float(x) for x in closes]
    if len(c) < 21:
        return {"long": [], "short": []}, {}
    last = c[-1]
    hi20 = max(c[-21:-1])
    lo20 = min(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1.0
    rng5 = (max(c[-5:]) - min(c[-5:])) / last if last else 0.0
    r = rsi(c)
    r_prev = rsi(c[:-1])
    long_tags, short_tags = [], []
    if last > hi20:
        long_tags.append("breakout_20d")
    if last < lo20:
        short_tags.append("breakdown_20d")
    if run > BULL_FLAG_RUN_PCT / 100.0 and rng5 < BULL_FLAG_RANGE_PCT / 100.0:
        long_tags.append("bull_flag")
    if run < -BULL_FLAG_RUN_PCT / 100.0 and rng5 < BULL_FLAG_RANGE_PCT / 100.0:
        short_tags.append("bear_flag")
    if c[-2] < ma20_prev and last > ma20:
        long_tags.append("reclaim_ma20")
    if c[-2] > ma20_prev and last < ma20:
        short_tags.append("reject_ma20")
    if r is not None and r < 30:
        long_tags.append("oversold")
    if r is not None and r > 70:
        short_tags.append("overbought")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        long_tags.append("oversold_bounce")
    if r_prev is not None and r_prev > 70 and last < c[-2]:
        short_tags.append("overbought_bounce")
    nums = {"rsi": r, "rsi_prev": r_prev, "high20": hi20, "low20": lo20, "ma20": ma20,
            "run_pct": run * 100.0, "range5_pct": rng5 * 100.0}
    return {"long": long_tags, "short": short_tags}, nums


SETUP_SCORE = {
    "breakout_20d": 2.0, "breakdown_20d": 2.0,
    "bull_flag": 1.5, "bear_flag": 1.5,
    "reclaim_ma20": 1.2, "reject_ma20": 1.2,
    "oversold": 1.0, "overbought": 1.0,
    "oversold_bounce": 1.8, "overbought_bounce": 1.8,
    "orb_breakout": 2.0, "orb_breakdown": 2.0,
    "level_breakout": 1.5, "level_breakdown": 1.5,
}


def setup_score(tags):
    return round(sum(SETUP_SCORE.get(t, 1.0) for t in tags), 2)


def session_bars(bars, start_utc, end_utc, now_utc, interval_min=5):
    """Completed bars whose start falls inside [start, end)."""
    out = []
    for b in bars:
        if not (start_utc <= b["t_utc"] < end_utc):
            continue
        if b["t_utc"] + datetime.timedelta(minutes=interval_min) > now_utc:
            continue
        out.append(b)
    out.sort(key=lambda b: b["t_utc"])
    return out


def opening_range(bars, start_utc, or_minutes, now_utc, interval_min=5, rest_end=None):
    """The session's first ``or_minutes`` of completed bars -> dict or None.

    Returns ``high``, ``low``, ``mid``, ``bars`` (the range bars), ``end``,
    ``rest`` (completed bars after the range, up to ``rest_end``) and ``ready``.
    """
    end = start_utc + datetime.timedelta(minutes=int(or_minutes))
    if now_utc < end + datetime.timedelta(minutes=interval_min):
        return None
    rng = session_bars(bars, start_utc, end, now_utc, interval_min)
    if len(rng) < max(1, int(or_minutes) // interval_min):
        return None
    high = max(b["h"] for b in rng)
    low = min(b["l"] for b in rng)
    rest = session_bars(bars, end, rest_end or (end + datetime.timedelta(days=1)),
                        now_utc, interval_min)
    return {"high": high, "low": low, "mid": (high + low) / 2.0,
            "bars": rng, "end": end, "rest": rest,
            "start": start_utc, "or_minutes": int(or_minutes)}


def first_break(rest, high, low, side=None, confirm="close"):
    """First completed bar that closes beyond the range on ``side``.

    ``confirm="close"`` uses the close (the original ORB rule); ``"wick"``
    uses the high/low, which triggers earlier and is noisier.
    """
    sides = [side] if side in ("buy", "sell") else ["buy", "sell"]
    for b in rest:
        for s in sides:
            if s == "buy":
                px = b["c"] if confirm == "close" else b["h"]
                if px > high:
                    return {"side": "buy", "price": px, "bar": b,
                            "at": b["t_utc"] + datetime.timedelta(minutes=5),
                            "level": high, "kind": "breakout"}
            else:
                px = b["c"] if confirm == "close" else b["l"]
                if px < low:
                    return {"side": "sell", "price": px, "bar": b,
                            "at": b["t_utc"] + datetime.timedelta(minutes=5),
                            "level": low, "kind": "breakdown"}
    return None


def pct_change(a, b):
    return (float(b) / float(a) - 1.0) * 100.0 if a else 0.0
