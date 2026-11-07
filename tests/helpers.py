"""Synthetic bars and fixtures for the offline tests.

A 60-session London history for EURUSD, ending on a fixed Wednesday morning,
with a clean opening-range breakout on the last day.
"""

import datetime
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import timeutil
from fx.fake import FakeGateway
from fx.symbols import SymbolSpec

# Wednesday 4 November 2026, 09:00 New York
NOW_NY = datetime.datetime(2026, 11, 4, 9, 0)
NOW_UTC = timeutil.ny_to_utc(NOW_NY)
SESSION = "london"
OR_MINUTES = 30


def make_m5(days=65, session=SESSION, start=1.1000, vol=0.00018, seed=7,
            breakout_side="buy", breakout_price=None):
    rng = random.Random(seed)
    today = NOW_NY.date()
    bars = []
    price = start
    for back in range(days, -1, -1):
        day = today - datetime.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        start_utc, end_utc = timeutil.session_window(day, session)
        n = int((end_utc - start_utc).total_seconds() // 300)
        is_last = (day == today)
        for i in range(n):
            t = start_utc + datetime.timedelta(minutes=5 * i)
            if is_last:
                if i < OR_MINUTES // 5:
                    price = 1.1000 + 0.0002 * (i % 3)
                else:
                    base = breakout_price if breakout_price is not None else (
                        1.1020 if breakout_side == "buy" else 1.0980)
                    price = base + 0.00005 * (i % 4)
            else:
                price += rng.gauss(0, vol)
            o = price
            c = price + rng.gauss(0, vol * 0.25)
            h = max(o, c) + abs(rng.gauss(0, vol * 0.2))
            l = min(o, c) - abs(rng.gauss(0, vol * 0.2))
            bars.append({"t": timeutil.utc_epoch(t, 2.0), "t_utc": t,
                         "o": round(o, 5), "h": round(h, 5), "l": round(l, 5),
                         "c": round(c, 5), "v": 100.0})
            price = c
    return bars


def make_flat_day_m5(days=30, start=1.1000, rise_per_bar=0.0001):
    """Weekdays of 03:00-17:00 NY bars: flat through London, then a steady rise.

    The move that reaches the target only happens *after* the London close, so
    a replay that stops at 12:00 NY never sees it, while a replay that runs to
    ``flat_by`` does. Used to test that the Time Machine holds to flat_by.
    """
    today = NOW_NY.date()
    bars = []
    for back in range(days, -1, -1):
        day = today - datetime.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        start_utc, _ = timeutil.session_window(day, "london")
        end_utc = timeutil.flat_by_utc(day, "17:00")
        london_close = timeutil.session_close_utc(day, "london")
        n = int((end_utc - start_utc).total_seconds() // 300)
        london_bars = int((london_close - start_utc).total_seconds() // 300)
        for i in range(n):
            t = start_utc + datetime.timedelta(minutes=5 * i)
            px = start if i <= london_bars else start + rise_per_bar * (i - london_bars)
            bars.append({"t": timeutil.utc_epoch(t, 2.0), "t_utc": t,
                         "o": round(px, 5), "h": round(px, 5), "l": round(px, 5),
                         "c": round(px, 5), "v": 100.0})
    return bars


def make_orb_days_m5(days=6, range_low=1.0990, range_high=1.1010, breakout="buy",
                     step=0.0002):
    """Weekdays of 03:00-16:55 NY. Earlier days stay inside the range; the
    last day breaks out at 03:30 and then moves ``step`` per bar.

    With the defaults the last day closes its first post-range bar at 1.1012,
    so a buy ORB enters there and the steady rise reaches the 1R target.
    """
    today = NOW_NY.date()
    bars = []
    for back in range(days, -1, -1):
        day = today - datetime.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        start_utc, _ = timeutil.session_window(day, "london")
        end_utc = timeutil.flat_by_utc(day, "16:55")
        n = int((end_utc - start_utc).total_seconds() // 300)
        is_last = (back == 0)
        for i in range(n):
            t = start_utc + datetime.timedelta(minutes=5 * i)
            if i < 6:
                px = range_low if i % 2 == 0 else range_high
            elif is_last:
                px = (range_high + step * (i - 5)) if breakout == "buy" \
                    else (range_low - step * (i - 5))
            else:
                px = (range_low + range_high) / 2.0
            bars.append({"t": timeutil.utc_epoch(t, 2.0), "t_utc": t,
                         "o": round(px, 5), "h": round(px, 5), "l": round(px, 5),
                         "c": round(px, 5), "v": 100.0})
    return bars


def make_d1(bars):
    """Aggregate M5 bars into one D1 bar per UTC date."""
    days = {}
    for b in bars:
        d = b["t_utc"].date()
        slot = days.setdefault(d, {"t_utc": b["t_utc"], "o": b["o"], "h": b["h"],
                                   "l": b["l"], "c": b["c"], "v": 0.0})
        slot["h"] = max(slot["h"], b["h"])
        slot["l"] = min(slot["l"], b["l"])
        slot["c"] = b["c"]
        slot["v"] += b["v"]
    out = []
    for d in sorted(days):
        row = days[d]
        row["t"] = timeutil.utc_epoch(row["t_utc"], 2.0)
        out.append(row)
    return out


def make_gateway(bars=None, balance=1000.0, specs=None):
    bars = bars if bars is not None else make_m5()
    g = FakeGateway(account={"balance": balance, "equity": balance}, bars={("EURUSD", "M5"): bars,
                                                                          ("EURUSD", "D1"): make_d1(bars)},
                    specs=specs)
    g.set_price("EURUSD", 1.1020, 1.1020)
    return g


def make_order(side="buy", volume=0.05, entry=1.1020, stop=1.1000, target=1.1040, **kw):
    o = {
        "symbol": "EURUSD", "side": side, "volume": volume, "type": "market",
        "entry": entry, "sl": stop, "tp": target, "comment": "test",
        "plan": {"entry": entry, "stop": stop, "target": target, "setup": "orb",
                 "session": SESSION, "max_loss": 10.0, "risk_pips": 20.0,
                 "reward_pips": 20.0, "reward_risk": 1.0},
    }
    o.update(kw)
    return o
