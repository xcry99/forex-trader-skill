"""Walk-forward backtest of the same rules the live pack trades.

The engine replays history bar by bar: the daily setups from completed D1
bars, the session opening range from M5 bars, the same stop/target arithmetic
(:mod:`fx.plan`), the same lot sizing (:mod:`fx.sizing`) and the same entry
window / daily caps from the risk config. It then prices every fill with the
broker's own ``order_calc_profit`` so JPY crosses and cross pairs come out
right.

What is deliberately *not* replayed:

* **News.** Headlines are not archived with their publication timestamps, so
  the news filter cannot be applied to the past. It is context-only by default
  anyway.
* **The Time Machine.** It is advisory and would multiply the runtime; the
  backtest answers "what did the rules do", not "what would the simulator have
  guessed".
* **Slippage.** Off by default. ``slippage_pips`` adds an adverse fill to the
  entry and to stop exits.

Assumptions worth knowing:

* A daily setup is acted on at ``no_entries_before`` (02:30 New York) because
  that is the first moment the live entry window allows it.
* An ORB entry fills at the close of the first completed M5 bar beyond the
  range, which is exactly the live rule.
* One position per symbol at a time, and a symbol+side can only be opened once
  a day (the live duplicate rule). Opposite sides on the same symbol are
  allowed after the first one closes, as in live.
* When a bar touches both the stop and the target, the stop wins (the same
  conservative tie-break as :func:`trader-timemachine.history_replay`).
* Bar prices are bid; a buy pays the spread on entry, a sell pays it on exit.
  The spread comes from each M5 bar and falls back to the symbol's current
  spread when the history says 0.
"""

import bisect
import copy
import datetime

from . import indicators, journal, plan as plan_mod, sizing, timeutil
from .symbols import format_pips, money

M5 = datetime.timedelta(minutes=5)


class BacktestError(Exception):
    pass


# ------------------------------------------------------------------ helpers

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _hhmm(text):
    if text in (None, "", False):
        return None
    h, m = [int(x) for x in str(text).split(":")]
    return datetime.time(h, m)


def in_window(now_utc, before, after):
    """The live entry window, as a boolean."""
    t = timeutil.to_ny(now_utc).time()
    if before and t < before:
        return False
    if after and t > after:
        return False
    return True


def _spread_price(bar, spec, override_pips=None):
    """Spread in price units: the bar's own, or the symbol's current one."""
    if override_pips is not None:
        return float(override_pips) * spec.pip_size
    points = float(bar.get("spread") or 0)
    if points <= 0 or points > 10000:
        points = float(spec.spread_points or 0)
    return points * spec.point


class Series(object):
    """A sorted bar list with cheap time slicing."""

    def __init__(self, bars):
        self.bars = sorted(bars, key=lambda b: b["t_utc"])
        self.times = [b["t_utc"] for b in self.bars]

    def __len__(self):
        return len(self.bars)

    def between(self, start, end):
        i = bisect.bisect_left(self.times, start)
        j = bisect.bisect_left(self.times, end)
        return self.bars[i:j]

    def before(self, cutoff):
        return self.bars[:bisect.bisect_right(self.times, cutoff)]


def load_history(gateway, symbols, start_utc, end_utc, warmup_days=120):
    """Fetch M5 and D1 bars for every symbol. Missing symbols go to errors."""
    hist, errors = {}, {}
    d1_start = start_utc - datetime.timedelta(days=int(warmup_days))
    for name in symbols:
        try:
            spec = gateway.symbol(name)
        except Exception as e:
            errors[str(name)] = str(e)
            continue
        try:
            m5 = gateway.rates_range(spec.name, "M5", start_utc, end_utc)
            d1 = gateway.rates_range(spec.name, "D1", d1_start, end_utc)
        except Exception as e:
            errors[spec.name] = str(e)
            continue
        if not m5 or not d1:
            errors[spec.name] = "no M5/D1 history in the period"
            continue
        hist[spec.name] = {"spec": spec, "m5": Series(m5), "d1": Series(d1)}
    return hist, errors


# ------------------------------------------------------------------ plans

def make_plan(entry, side, spec, setup, atr=None, or_high=None, or_low=None):
    """The live plan: or-mid for ORB (ATR fallback), 1.5 x ATR(D1) for daily."""
    if setup == "orb":
        try:
            return plan_mod.compute(entry, side, "or_mid", None, "r", 1.0, spec,
                                    or_high=or_high, or_low=or_low, atr=atr)
        except plan_mod.PlanError:
            pass
    try:
        return plan_mod.compute(entry, side, "atr", 1.5, "r", 1.0, spec, atr=atr)
    except plan_mod.PlanError:
        return None


# ------------------------------------------------------------------ exits

def _hit(price, bar, reason, spec):
    return {"price": spec.round_price(price), "time": bar["t_utc"] + M5, "reason": reason}


def simulate_exit(side, entry, stop, target, bars, spec, spread_pips=None,
                  slippage_pips=0.0):
    """Walk the bars after the entry until stop, target or the last bar.

    ``bars`` must already be limited to the holding window (entry -> flat_by).
    The stop wins when one bar touches both levels.
    """
    long = side == "buy"
    slip = float(slippage_pips or 0) * spec.pip_size
    for b in bars:
        spread = _spread_price(b, spec, spread_pips)
        if long:
            if b["o"] <= stop:
                return _hit(b["o"] - slip, b, "stop_gap", spec)
            if b["l"] <= stop:
                return _hit(stop - slip, b, "stop", spec)
            if b["o"] >= target:
                return _hit(b["o"], b, "target_gap", spec)
            if b["h"] >= target:
                return _hit(target, b, "target", spec)
        else:
            if b["o"] + spread >= stop:
                return _hit(b["o"] + spread + slip, b, "stop_gap", spec)
            if b["h"] + spread >= stop:
                return _hit(stop + slip, b, "stop", spec)
            if b["o"] + spread <= target:
                return _hit(b["o"] + spread, b, "target_gap", spec)
            if b["l"] + spread <= target:
                return _hit(target, b, "target", spec)
    if not bars:
        return None
    b = bars[-1]
    spread = _spread_price(b, spec, spread_pips)
    price = b["c"] if long else b["c"] + spread
    return _hit(price, b, "flat_by", spec)


# ------------------------------------------------------------------ candidates

def _daily_candidates(cfg, hist, day, entry_start, session, spread_pips):
    out = []
    cutoff = entry_start - datetime.timedelta(hours=24)
    for sym, h in hist.items():
        spec = h["spec"]
        completed = h["d1"].before(cutoff)
        if len(completed) < 21:
            continue
        closes = [b["c"] for b in completed]
        tags, _nums = indicators.find_setups(closes)
        sides = (("buy", tags["long"]), ("sell", tags["short"]))
        if not any(t for _, t in sides):
            continue
        bars = h["m5"].between(entry_start, entry_start + datetime.timedelta(hours=12))
        if not bars:
            continue
        bar = bars[0]
        atr = indicators.atr(completed[-40:], 14)
        if not atr:
            continue
        spread = _spread_price(bar, spec, spread_pips)
        for side, found in sides:
            if not found:
                continue
            entry = bar["o"] + (spread if side == "buy" else 0.0)
            p = make_plan(entry, side, spec, "daily", atr=atr)
            if not p:
                continue
            out.append({
                "symbol": sym, "side": side, "setup": "daily",
                "setups": list(found), "score": indicators.setup_score(found),
                "entry": p["entry"], "stop": p["stop"], "target": p["target"],
                "entry_time": bar["t_utc"], "entry_bar": bar,
                "session": session,
                "day": day.isoformat(), "atr": atr, "spread_pips": spec.pips(spread),
                "plan": p,
            })
    return out


def _orb_candidates(cfg, hist, day, session, or_minutes, confirm, spread_pips):
    start, end = timeutil.session_window(day, session)
    out = []
    for sym, h in hist.items():
        spec = h["spec"]
        bars = h["m5"].between(start, end)
        need = max(1, int(or_minutes) // 5)
        if len(bars) < need + 2:
            continue
        rng = indicators.opening_range(bars, start, or_minutes, end, 5, rest_end=end)
        if not rng:
            continue
        br = indicators.first_break(rng["rest"], rng["high"], rng["low"], confirm=confirm)
        if not br:
            continue
        bar = br["bar"]
        spread = _spread_price(bar, spec, spread_pips)
        side = br["side"]
        entry = br["price"] + (spread if side == "buy" else 0.0)
        atr = indicators.atr(rng["bars"], 14)
        p = make_plan(entry, side, spec, "orb", atr=atr,
                      or_high=rng["high"], or_low=rng["low"])
        if not p:
            continue
        tag = "orb_breakout" if side == "buy" else "orb_breakdown"
        out.append({
            "symbol": sym, "side": side, "setup": "orb", "setups": [tag],
            "score": indicators.setup_score([tag]),
            "entry": p["entry"], "stop": p["stop"], "target": p["target"],
            "entry_time": bar["t_utc"] + M5, "entry_bar": bar,
            "session": session, "day": day.isoformat(),
            "or_high": rng["high"], "or_low": rng["low"], "atr": atr,
            "spread_pips": spec.pips(spread), "plan": p,
        })
    return out


def _merge(cands):
    """Same (symbol, side) twice -> one candidate; the daily plan wins."""
    merged = {}
    for c in cands:
        key = (c["symbol"], c["side"])
        if key not in merged:
            merged[key] = dict(c)
            continue
        m = merged[key]
        m["setups"] = sorted(set(m["setups"]) | set(c["setups"]))
        m["score"] = indicators.setup_score(m["setups"])
        for k in ("or_high", "or_low"):
            if m.get(k) is None:
                m[k] = c.get(k)
    return list(merged.values())


# ------------------------------------------------------------------ engine

def run(cfg, gateway, symbols=None, days=90, session=None, or_minutes=None, now=None,
        spread_pips=None, slippage_pips=0.0, budget=None, risk_usd=None,
        daily_loss_usd=None, max_trades=None, daily=True, orb=True, confirm=None,
        warmup_days=120, hist=None):
    """Replay the live rules over the last ``days`` calendar days.

    Pass ``hist`` (from :func:`load_history`) to reuse already-loaded bars
    across runs; only the requested symbols are kept. The comparison command
    uses this so a grid of variants reads MetaTrader once, not once per run.
    """
    now = now or timeutil.utc_now()
    strat = cfg.get("strategy") or {}
    rcfg = cfg.get("risk") or {}
    acfg = cfg.get("account") or {}
    orbcfg = strat.get("orb") or {}
    session = session or orbcfg.get("session") or "london"
    or_minutes = int(or_minutes or orbcfg.get("or_minutes") or 30)
    confirm = confirm or orbcfg.get("confirm") or "close"
    budget = float(budget if budget is not None else (acfg.get("budget") or 1000))
    max_trades = int(max_trades if max_trades is not None else (rcfg.get("max_trades_per_day") or 3))
    daily_loss = float(daily_loss_usd if daily_loss_usd is not None
                       else (rcfg.get("max_daily_loss_usd") or 30))
    before = _hhmm(rcfg.get("no_entries_before"))
    after = _hhmm(rcfg.get("no_entries_after"))
    flat_by = rcfg.get("flat_by") or "16:55"
    min_rr = _f(rcfg.get("min_reward_risk"))
    allow_short = bool(rcfg.get("allow_short", True))

    cfg_eff = cfg
    if risk_usd is not None:
        cfg_eff = copy.deepcopy(cfg)
        cfg_eff.setdefault("risk", {})["max_loss_per_trade_usd"] = float(risk_usd)

    symbols = [str(s).upper() for s in (symbols or strat.get("watchlist") or [])]
    if not symbols:
        raise BacktestError("no symbols: give --symbols or set strategy.watchlist")
    start = now - datetime.timedelta(days=int(days))
    if hist is None:
        hist, errors = load_history(gateway, symbols, start, now, warmup_days=warmup_days)
    else:
        wanted = set(symbols)
        hist = {k: v for k, v in hist.items() if k in wanted}
        errors = {}
    if not hist:
        raise BacktestError("no history for %s: %s" % (", ".join(symbols), errors))

    balance = budget
    used_margin = 0.0
    trades, skipped = [], []
    first_day = timeutil.to_ny(start).date()
    last_day = timeutil.to_ny(now).date()
    day = first_day
    trading_days = 0

    while day <= last_day:
        is_open, _why = timeutil.market_open(
            timeutil.ny_to_utc(datetime.datetime.combine(day, datetime.time(9, 0))))
        if not is_open:
            day += datetime.timedelta(days=1)
            continue
        trading_days += 1

        daily_start = timeutil.ny_to_utc(
            datetime.datetime.combine(day, before or datetime.time(0, 0)))
        cands = []
        if daily:
            cands.extend(_daily_candidates(cfg, hist, day, daily_start, session, spread_pips))
        if orb:
            cands.extend(_orb_candidates(cfg, hist, day, session, or_minutes, confirm, spread_pips))
        cands = _merge(cands)
        cands.sort(key=lambda c: (c["entry_time"], c["symbol"],
                                  0 if c["setup"] == "daily" else 1))

        placed = set()
        entries_today = 0
        risk_today = 0.0
        open_by_symbol = {}

        for c in cands:
            if c["entry_time"] > now:
                continue
            if not in_window(c["entry_time"], before, after):
                skipped.append(_skip(c, "window", "entry at %s NY is outside %s-%s"
                                     % (timeutil.to_ny(c["entry_time"]).strftime("%H:%M"),
                                        before or "00:00", after or "24:00")))
                continue
            if c["side"] == "sell" and not allow_short:
                skipped.append(_skip(c, "short", "allow_short is off"))
                continue
            key = (c["symbol"], c["side"])
            if key in placed:
                skipped.append(_skip(c, "duplicate", "a %s %s was already opened today"
                                     % (c["side"], c["symbol"])))
                continue
            if c["symbol"] in open_by_symbol:
                skipped.append(_skip(c, "position", "%s already has an open position" % c["symbol"]))
                continue
            if entries_today >= max_trades:
                skipped.append(_skip(c, "trades/day", "%d entries today, limit %d"
                                     % (entries_today, max_trades)))
                continue
            spec = hist[c["symbol"]]["spec"]
            if c["setup"] == "orb" and min_rr and c["plan"]["reward_risk"] < min_rr - 1e-9:
                skipped.append(_skip(c, "reward:risk", "%.2f under the %.2f minimum"
                                     % (c["plan"]["reward_risk"], min_rr)))
                continue

            flat = timeutil.flat_by_utc(day, flat_by)
            if flat <= c["entry_time"]:
                flat = timeutil.flat_by_utc(day + datetime.timedelta(days=1), flat_by)
            # Never replay past "now": a backtest that ends today must not use
            # bars the live agent has not seen yet.
            horizon = min(flat, now)

            spread = _spread_price(c["entry_bar"], spec, spread_pips)
            if c["side"] == "buy":
                tick = {"bid": c["entry"] - spread, "ask": c["entry"]}
            else:
                tick = {"bid": c["entry"], "ask": c["entry"] + spread}
            acct = {"balance": balance, "equity": balance, "margin": used_margin,
                    "margin_free": balance - used_margin, "currency": "USD"}
            try:
                s = sizing.size_plan(c["plan"], cfg_eff, spec, gateway, account=acct,
                                     budget=budget, tick=tick, order_type="market")
            except sizing.SizeError as e:
                skipped.append(_skip(c, "size", str(e)))
                continue
            if s.get("refusal"):
                skipped.append(_skip(c, "size", s["refusal"]))
                continue
            if risk_today + s["max_loss"] > daily_loss + 0.005:
                skipped.append(_skip(c, "daily loss", "%s at risk today + %s > %s limit"
                                     % (money(risk_today), money(s["max_loss"]), money(daily_loss))))
                continue
            if c["setup"] == "daily" and min_rr and s["reward_risk"] < min_rr - 1e-9:
                skipped.append(_skip(c, "reward:risk", "%.2f under the %.2f minimum"
                                     % (s["reward_risk"], min_rr)))
                continue

            # daily entries fill at the bar open and can be stopped inside that
            # same bar; ORB entries fill at the signal close and start after it.
            bars = hist[c["symbol"]]["m5"].between(c["entry_time"], horizon)
            if not bars:
                skipped.append(_skip(c, "no bars", "no M5 bars between the entry and flat_by"))
                continue
            exit_doc = simulate_exit(c["side"], s["entry"], s["stop"], s["target"], bars,
                                     spec, spread_pips=spread_pips,
                                     slippage_pips=slippage_pips)
            if not exit_doc:
                skipped.append(_skip(c, "no bars", "nothing to replay after the entry"))
                continue

            net = gateway.calc_profit(c["side"], c["symbol"], s["volume"],
                                      s["entry"], exit_doc["price"])
            net = round(float(net or 0.0), 2)
            pips = spec.pips((exit_doc["price"] - s["entry"]) *
                             (1.0 if c["side"] == "buy" else -1.0))
            risk = s["max_loss"] or 0.0
            trade = {
                "id": "bt-%04d" % (len(trades) + 1),
                "symbol": c["symbol"], "side": c["side"], "volume": s["volume"],
                "entry_price": spec.round_price(s["entry"]),
                "exit_price": spec.round_price(exit_doc["price"]),
                "stop": spec.round_price(s["stop"]), "target": spec.round_price(s["target"]),
                "pips": round(pips, 2), "gross": net, "commission": 0.0, "swap": 0.0,
                "net": net, "risk_usd": risk,
                "r_multiple": round(net / risk, 3) if risk else None,
                "setup": c["setup"], "setups": c["setups"], "score": c["score"],
                "session": c["session"], "confidence": None,
                "opened": timeutil.iso(c["entry_time"]),
                "closed": timeutil.iso(exit_doc["time"]),
                "exit_reason": _exit_reason(exit_doc["reason"]),
                "hold_minutes": int((exit_doc["time"] - c["entry_time"]).total_seconds() // 60),
                "spread_pips": round(spec.pips(spread), 2),
                "margin": s["margin"], "bound_by": s["bound_by"],
            }
            trades.append(trade)
            balance = round(balance + net, 2)
            used_margin = round(max(used_margin - float(s["margin"] or 0), 0.0), 2)
            entries_today += 1
            risk_today += risk
            placed.add(key)
            open_by_symbol[c["symbol"]] = trade

        day += datetime.timedelta(days=1)

    summary = journal.metrics(trades, budget)
    summary.update({
        "days": int(days),
        "trading_days": trading_days,
        "trades_per_day": round(len(trades) / float(trading_days), 2) if trading_days else 0.0,
        "skipped": len(skipped),
        "skip_reasons": _skip_counts(skipped),
        "period": {"from": first_day.isoformat(), "to": last_day.isoformat(), "days": int(days)},
        "params": {
            "symbols": list(hist.keys()), "session": session, "or_minutes": or_minutes,
            "confirm": confirm, "daily_setups": bool(daily), "orb_setups": bool(orb),
            "budget": budget, "risk_usd": (cfg_eff.get("risk") or {}).get("max_loss_per_trade_usd"),
            "max_daily_loss_usd": daily_loss, "max_trades_per_day": max_trades,
            "spread": ("override %.1f pips" % spread_pips) if spread_pips is not None
                      else "from each M5 bar, fallback to the symbol's current spread",
            "slippage_pips": float(slippage_pips or 0),
            "flat_by": flat_by, "window": "%s-%s NY" % (before or "00:00", after or "24:00"),
        },
    })
    return {
        "as_of": timeutil.iso(now), "kind": "backtest",
        "summary": summary, "trades": trades, "skipped": skipped, "errors": errors,
    }


def _exit_reason(reason):
    if reason.startswith("stop"):
        return "stop"
    if reason.startswith("target"):
        return "target"
    return "time"


def _skip(c, stage, reason):
    return {"day": c["day"], "symbol": c["symbol"], "side": c["side"],
            "setup": c["setup"], "stage": stage, "reason": reason}


def _skip_counts(skipped):
    out = {}
    for s in skipped:
        out[s["stage"]] = out.get(s["stage"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ------------------------------------------------------------------ report

def report(doc):
    m = doc["summary"]
    p = m["params"]
    lines = ["FOREX BACKTEST  (walk-forward, same rules as live)", "=" * 68]
    lines.append("Period        %s -> %s (%d calendar days, %d trading days)"
                 % (m["period"]["from"], m["period"]["to"], m["period"]["days"],
                    m["trading_days"]))
    lines.append("Symbols       %s" % ", ".join(p["symbols"]))
    lines.append("Setups        %s" % " + ".join(
        x for x in (("daily" if p["daily_setups"] else ""), ("orb" if p["orb_setups"] else "")) if x))
    lines.append("Session       %s, %d-minute opening range (%s)"
                 % (p["session"], p["or_minutes"], p["confirm"]))
    lines.append("Risk          %s/trade, %s/day, %d trades/day, budget %s"
                 % (money(p["risk_usd"]), money(p["max_daily_loss_usd"]),
                    p["max_trades_per_day"], money(p["budget"])))
    lines.append("Spread        %s" % p["spread"])
    lines.append("")
    lines.append(journal.report(m))
    lines.append("")
    lines.append("Trades/day    %.2f over %d trading days" % (m["trades_per_day"], m["trading_days"]))
    lines.append("Skipped       %d (%s)" % (m["skipped"],
                 ", ".join("%s=%d" % kv for kv in m["skip_reasons"].items()) or "none"))
    if doc.get("errors"):
        lines.append("No history    %s" % ", ".join(sorted(doc["errors"])))
    return "\n".join(lines)


def trades_table(trades, limit=None):
    rows = []
    shown = trades[-limit:] if limit else trades
    for t in shown:
        rows.append([t["closed"][:16], t["symbol"], t["side"].upper(), t["setup"],
                     "%.2f" % t["volume"], t["entry_price"], t["exit_price"],
                     "%.1f" % t["pips"], "%.2f" % t["net"], t["exit_reason"]])
    return rows
