#!/usr/bin/env python3
"""Chart scanner for FX pairs.

    python scan.py universe                 # the configured watchlist + specs
    python scan.py universe --all           # every FX symbol the broker offers
    python scan.py daily                    # 20-day breakout / flag / MA reclaim / RSI
    python scan.py orb --session london     # opening-range breakouts, long and short
    python scan.py all                      # both, merged

The daily screen keeps the original five setups and adds their short-side
mirrors. The opening range is the same rule as the stock pack's 9:30-9:45
range, moved to a Forex session (London 03:00 ET by default).
"""

import argparse
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, indicators, store, timeutil


def _specs(cfg, gateway, all_symbols=False):
    wanted = [str(s).upper() for s in ((cfg.get("strategy") or {}).get("watchlist") or [])]
    specs, errors = [], {}
    if all_symbols:
        for s in gateway.symbols():
            if s.is_forex and s.trade_mode == 4:
                specs.append(s)
        specs.sort(key=lambda s: s.name)
    else:
        for w in wanted:
            try:
                specs.append(gateway.symbol(w))
            except Exception as e:
                errors[w] = str(e)
    return specs, errors


def cmd_universe(args, cfg, gateway):
    specs, errors = _specs(cfg, gateway, args.all)
    rows, doc_specs = [], []
    for s in specs:
        spread = s.spread_pips
        if args.live_spreads:
            try:
                spread = gateway.tick(s.name)["spread_pips"]
            except Exception:
                pass
        d = s.as_dict()
        d["spread_pips"] = round(spread, 2)
        doc_specs.append(d)
        rows.append([s.name, s.description[:32], s.digits, "%g" % s.pip_size,
                     "%g" % s.contract_size, "%s-%s" % (s.volume_min, s.volume_max),
                     "%.1f" % spread, "%s/%s" % (s.currency_base, s.currency_profit)])
    doc = {"as_of": timeutil.iso(timeutil.utc_now()), "count": len(doc_specs),
           "symbols": doc_specs, "errors": errors}
    if not args.no_write:
        store.save_json(cfg, "universe.json", doc)
    text = cli.table(rows, ["SYMBOL", "DESCRIPTION", "DIG", "PIP", "CONTRACT", "LOTS", "SPREAD", "PAIR"])
    text += "\n\n%d symbols%s" % (len(rows), "" if args.no_write else " -> trader/universe.json")
    if errors:
        text += "\nnot offered: " + ", ".join(sorted(errors))
    cli.emit(doc, args, text)


def _daily_hits(cfg, gateway, now, symbols):
    min_bars = int((cfg.get("strategy") or {}).get("min_daily_bars") or 30)
    hits, counts = [], {"requested": len(symbols), "fetched": 0, "short": 0, "no_setups": 0, "hits": 0}
    errors = {}
    for name in symbols:
        try:
            spec = gateway.symbol(name)
            bars = indicators.completed_daily_bars(gateway.rates(spec.name, "D1", 220), now)
        except Exception as e:
            errors[str(name)] = str(e)
            continue
        counts["fetched"] += 1
        if len(bars) < min_bars:
            counts["short"] += 1
            continue
        closes = [b["c"] for b in bars]
        tags, nums = indicators.find_setups(closes)
        atr = indicators.atr(bars[-40:], 14)
        atr_pips = spec.pips(atr) if atr else None
        spread = spec.spread_pips
        any_side = False
        for side, key in (("buy", "long"), ("sell", "short")):
            if not tags[key]:
                continue
            any_side = True
            hits.append({
                "symbol": spec.name, "side": side, "setup": "daily",
                "setups": tags[key], "score": indicators.setup_score(tags[key]),
                "price": round(closes[-1], spec.digits),
                "close_date": bars[-1]["t_utc"].date().isoformat(),
                "rsi": round(nums["rsi"], 1) if nums.get("rsi") is not None else None,
                "ma20": round(nums["ma20"], spec.digits),
                "high20": round(nums["high20"], spec.digits),
                "low20": round(nums["low20"], spec.digits),
                "run_pct": round(nums["run_pct"], 2),
                "range5_pct": round(nums["range5_pct"], 2),
                "atr_pips": round(atr_pips, 1) if atr_pips else None,
                "spread_pips": round(spread, 2),
                "digits": spec.digits,
            })
        if not any_side:
            counts["no_setups"] += 1
    hits.sort(key=lambda h: (-h["score"], -(h.get("atr_pips") or 0), h["symbol"]))
    counts["hits"] = len(hits)
    return hits, counts, errors


def _orb_hits(cfg, gateway, now, symbols, session, or_minutes, confirm="close"):
    day = timeutil.session_day(now, session)
    start, end = timeutil.session_window(day, session)
    if now < start:
        day = day - datetime.timedelta(days=1)
        start, end = timeutil.session_window(day, session)
    hits, counts = [], {"requested": len(symbols), "fetched": 0, "pending": 0,
                        "no_range": 0, "hits": 0}
    errors = {}
    for name in symbols:
        try:
            spec = gateway.symbol(name)
            bars = gateway.rates(spec.name, "M5", 2500)
        except Exception as e:
            errors[str(name)] = str(e)
            continue
        counts["fetched"] += 1
        rng = indicators.opening_range(bars, start, or_minutes, now, 5, rest_end=end)
        if not rng:
            counts["no_range"] += 1
            continue
        br = indicators.first_break(rng["rest"], rng["high"], rng["low"], confirm=confirm)
        if not br:
            counts["pending"] += 1
            continue
        side = br["side"]
        tag = "orb_breakout" if side == "buy" else "orb_breakdown"
        hits.append({
            "symbol": spec.name, "side": side, "setup": "orb",
            "setups": [tag], "score": indicators.setup_score([tag]),
            "price": round(br["price"], spec.digits),
            "or_high": round(rng["high"], spec.digits),
            "or_low": round(rng["low"], spec.digits),
            "or_mid": round(rng["mid"], spec.digits),
            "or_minutes": or_minutes,
            "at": timeutil.iso(br["at"]),
            "bar": timeutil.iso(br["bar"]["t_utc"]),
            "session": session,
            "day": day.isoformat(),
            "level": round(br["level"], spec.digits),
            "pct_over": round((br["price"] / br["level"] - 1.0) * 100.0, 3) if br["level"] else 0.0,
            "atr_pips": round(spec.pips(indicators.atr(rng["bars"], 14) or 0), 1) or None,
            "spread_pips": round(spec.spread_pips, 2),
            "digits": spec.digits,
        })
    hits.sort(key=lambda h: (-h["score"], -(h.get("atr_pips") or 0), h["symbol"]))
    counts["hits"] = len(hits)
    return hits, counts, errors, day


def _rows(hits):
    return [[h["symbol"], h["side"].upper(), "+".join(h["setups"]), "%.1f" % h["score"],
             h["price"], h.get("rsi", ""), h.get("atr_pips", ""), h.get("spread_pips", "")]
            for h in hits]


def cmd_daily(args, cfg, gateway, now):
    specs, _ = _specs(cfg, gateway)
    symbols = [s.name for s in specs]
    hits, counts, errors = _daily_hits(cfg, gateway, now, symbols)
    doc = {"kind": "daily", "as_of": timeutil.iso(now), "hits": hits, "counts": counts, "errors": errors}
    if not args.no_write:
        store.save_json(cfg, "scan.json", doc)
    text = cli.table(_rows(hits), ["SYMBOL", "SIDE", "SETUPS", "SCORE", "PRICE", "RSI", "ATRp", "SPRD"])
    text += "\n\n%d hit(s) from %d scanned%s" % (counts["hits"], counts["fetched"],
                                                 "" if args.no_write else " -> trader/scan.json")
    if errors:
        text += "\nskipped: " + ", ".join("%s (%s)" % (k, v) for k, v in list(errors.items())[:4])
    cli.emit(doc, args, text)


def cmd_orb(args, cfg, gateway, now):
    is_open, why = timeutil.market_open(now)
    if not is_open:
        print("market closed: %s. The opening-range scan needs a live session; "
              "pin a weekday with --now to review one." % why)
        return 0
    specs, _ = _specs(cfg, gateway)
    symbols = [s.name for s in specs]
    strat = (cfg.get("strategy") or {}).get("orb") or {}
    session = args.session or strat.get("session") or "london"
    or_minutes = args.or_minutes or int(strat.get("or_minutes") or 30)
    hits, counts, errors, day = _orb_hits(cfg, gateway, now, symbols, session, or_minutes,
                                          args.confirm or strat.get("confirm") or "close")
    doc = {"kind": "orb", "as_of": timeutil.iso(now), "session": session, "day": day.isoformat(),
           "or_minutes": or_minutes, "hits": hits, "counts": counts, "errors": errors}
    if not args.no_write:
        store.save_json(cfg, "scan.json", doc)
    text = cli.table(_rows(hits), ["SYMBOL", "SIDE", "SETUPS", "SCORE", "PRICE", "RSI", "ATRp", "SPRD"])
    text += ("\n\n%s %d-minute opening range, %s session (%s): %d breakout(s), %d still inside"
             % (timeutil.day_label(day), or_minutes, session, timeutil.iso(now),
                counts["hits"], counts["pending"]))
    if not args.no_write:
        text += " -> trader/scan.json"
    cli.emit(doc, args, text)


def cmd_all(args, cfg, gateway, now):
    specs, _ = _specs(cfg, gateway)
    symbols = [s.name for s in specs]
    strat = (cfg.get("strategy") or {}).get("orb") or {}
    session = args.session or strat.get("session") or "london"
    or_minutes = args.or_minutes or int(strat.get("or_minutes") or 30)
    dh, dc, de = _daily_hits(cfg, gateway, now, symbols)
    is_open, why = timeutil.market_open(now)
    if is_open:
        oh, oc, oe, day = _orb_hits(cfg, gateway, now, symbols, session, or_minutes,
                                    args.confirm or strat.get("confirm") or "close")
    else:
        oh = []
        oc = {"requested": len(symbols), "fetched": 0, "pending": 0, "no_range": 0,
              "hits": 0, "closed": why}
        oe = {}
        day = timeutil.to_ny(now).date()
    merged = {}
    for h in dh + oh:
        key = (h["symbol"], h["side"])
        if key in merged:
            merged[key]["setups"] = sorted(set(merged[key]["setups"]) | set(h["setups"]))
            merged[key]["score"] = indicators.setup_score(merged[key]["setups"])
            merged[key].setdefault("orb", {}).update({k: v for k, v in h.items() if k.startswith("or_")})
            merged[key]["price"] = h["price"]
        else:
            merged[key] = dict(h)
    hits = sorted(merged.values(), key=lambda h: (-h["score"], -(h.get("atr_pips") or 0), h["symbol"]))
    doc = {"kind": "all", "as_of": timeutil.iso(now), "session": session, "day": day.isoformat(),
           "or_minutes": or_minutes, "hits": hits,
           "counts": {"daily": dc, "orb": oc}, "errors": {"daily": de, "orb": oe}}
    if not args.no_write:
        store.save_json(cfg, "scan.json", doc)
    text = cli.table(_rows(hits), ["SYMBOL", "SIDE", "SETUPS", "SCORE", "PRICE", "RSI", "ATRp", "SPRD"])
    text += "\n\n%d candidate(s): %d daily, %d opening-range%s" % (
        len(hits), dc["hits"], oc["hits"], "" if args.no_write else " -> trader/scan.json")
    if not is_open:
        text += "\nopening range skipped: market closed (%s)" % why
    cli.emit(doc, args, text)


def main(argv=None):
    p = argparse.ArgumentParser(description="FX chart scanner")
    sub = p.add_subparsers(dest="cmd", required=True)

    u = sub.add_parser("universe", help="list the FX symbols and contract specs")
    cli.add_common(u, now=False)
    u.add_argument("--all", action="store_true", help="every FX symbol, not just the watchlist")
    u.add_argument("--live-spreads", action="store_true")
    u.add_argument("--no-write", action="store_true")

    d = sub.add_parser("daily", help="daily setups, long and short")
    cli.add_common(d)
    d.add_argument("--no-write", action="store_true")

    o = sub.add_parser("orb", help="session opening-range breakouts")
    cli.add_common(o)
    o.add_argument("--session", default=None, help="asia, london, overlap, newyork (default london)")
    o.add_argument("--or-minutes", type=int, default=None)
    o.add_argument("--confirm", choices=("close", "wick"), default=None)
    o.add_argument("--no-write", action="store_true")

    a = sub.add_parser("all", help="daily + opening range")
    cli.add_common(a)
    a.add_argument("--session", default=None)
    a.add_argument("--or-minutes", type=int, default=None)
    a.add_argument("--confirm", choices=("close", "wick"), default=None)
    a.add_argument("--no-write", action="store_true")

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        if args.cmd == "universe":
            cmd_universe(args, cfg, gateway)
        elif args.cmd == "daily":
            cmd_daily(args, cfg, gateway, now)
        elif args.cmd == "orb":
            cmd_orb(args, cfg, gateway, now)
        else:
            cmd_all(args, cfg, gateway, now)
    finally:
        gateway.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
