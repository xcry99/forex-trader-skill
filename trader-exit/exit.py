#!/usr/bin/env python3
"""Exit planner: set the stop and the target before the entry.

    python exit.py plan --from-entry --stop or-mid --target 1R --write
    python exit.py plan --symbol EURUSD --side sell --stop atr:1.5 --target 2R --write
    python exit.py show

The original rules are kept: the stop can be the opening-range midpoint, its
low, a price, a percentage or an ATR multiple, and the target is an R
multiple or a price. The short side mirrors every one of them.
"""

import argparse
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, indicators, plan as plan_mod, store, timeutil
from fx.symbols import format_pips, format_price


def _entry_source(cfg, args):
    if args.from_entry:
        doc = store.load_json(cfg, "entry.json")
        if not doc:
            raise SystemExit("no trader/entry.json: run trader-entry first")
        entries = [e for e in (doc.get("entries") or []) if e.get("status") == "triggered"]
        if args.symbol:
            entries = [e for e in entries if e["symbol"].upper() == args.symbol.upper()]
        if not entries:
            raise SystemExit("trader/entry.json has no triggered entry%s"
                             % (" for " + args.symbol if args.symbol else ""))
        return entries[0]
    if args.from_scan:
        doc = store.load_json(cfg, "scan.json")
        if not doc:
            raise SystemExit("no trader/scan.json: run trader-scanner first")
        hits = doc.get("hits") or []
        if args.symbol:
            hits = [h for h in hits if h["symbol"].upper() == args.symbol.upper()]
        if not hits:
            raise SystemExit("trader/scan.json has no hit%s"
                             % (" for " + args.symbol if args.symbol else ""))
        return hits[0]
    return None


def _atr_for(gateway, spec, now, session, entry=None, timeframe="M5"):
    if str(timeframe).upper() == "D1":
        bars = indicators.completed_daily_bars(gateway.rates(spec.name, "D1", 220), now)
        use = bars[-40:]
        return indicators.atr(use, 14), len(use)
    bars = gateway.rates(spec.name, "M5", 1000)
    start, end = timeutil.session_window(timeutil.session_day(now, session), session)
    sb = indicators.session_bars(bars, start, end, now, 5)
    use = sb if len(sb) >= 3 else bars[-40:]
    a = indicators.atr(use, 14)
    return a, len(use)


def cmd_plan(args, cfg, gateway, now):
    src = _entry_source(cfg, args)
    symbol = (src or {}).get("symbol") or args.symbol
    if not symbol:
        raise SystemExit("give --symbol, --from-entry or --from-scan")
    spec = gateway.symbol(symbol)
    side = args.side or (src or {}).get("side") or "buy"
    if side not in ("buy", "sell"):
        raise SystemExit("side must be buy or sell")
    session = args.session or (src or {}).get("session") or ((cfg.get("strategy") or {}).get("orb") or {}).get("session") or "london"

    entry = args.entry
    if entry is None:
        entry = (src or {}).get("price")
    if entry is None:
        t = gateway.tick(spec.name)
        entry = t["ask"] if side == "buy" else t["bid"]
    entry = spec.round_price(entry)

    or_high = args.or_high if args.or_high is not None else (src or {}).get("or_high")
    or_low = args.or_low if args.or_low is not None else (src or {}).get("or_low")
    setup = (src or {}).get("setup") or args.setup or "manual"
    atr, atr_n = _atr_for(gateway, spec, now, session, entry,
                          timeframe="D1" if setup == "daily" else "M5")

    try:
        stop_rule, stop_param = plan_mod.parse_stop(args.stop)
        target_rule, target_param = plan_mod.parse_target(args.target)
        p = plan_mod.compute(entry, side, stop_rule, stop_param, target_rule, target_param,
                             spec, or_high=or_high, or_low=or_low, atr=atr, atr_bars=atr_n)
    except plan_mod.PlanError as e:
        raise SystemExit(str(e))

    doc = {
        "symbol": spec.name, "side": side, "setup": setup,
        "setups": (src or {}).get("setups") or [],
        "session": session, "date": timeutil.to_ny(now).date().isoformat(),
        "entry_time": timeutil.iso(now),
        "flat_by": args.flat_by or ((cfg.get("risk") or {}).get("flat_by") or "16:55"),
        "or_high": spec.round_price(or_high) if or_high is not None else None,
        "or_low": spec.round_price(or_low) if or_low is not None else None,
        "confidence": (src or {}).get("confidence"),
        **p,
    }
    if args.write:
        store.save_json(cfg, "plan.json", doc)
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        print("%s %s  %s" % (doc["side"].upper(), doc["symbol"], doc["setup"]))
        print("  entry    %s" % format_price(doc["entry"], spec))
        print("  stop     %s  (%s, %s)" % (format_price(doc["stop"], spec), doc["stop_label"],
                                            format_pips(doc["risk_pips"]) + " pips"))
        print("  target   %s  (%s, %s)" % (format_price(doc["target"], spec), doc["target_label"],
                                            format_pips(doc["reward_pips"]) + " pips"))
        print("  risk     %s pips = 1R, reward:risk %.2f" % (format_pips(doc["risk_pips"]), doc["reward_risk"]))
        if doc.get("atr"):
            print("  ATR(14)  %s pips over %d bars" % (format_pips(doc["atr_pips"]), doc["atr_bars"]))
        if doc.get("or_high") is not None:
            print("  range    %s - %s" % (doc["or_high"], doc["or_low"]))
        print("  flat by  %s New York" % doc["flat_by"])
        print("  -> trader/plan.json" if args.write else "  (not written: add --write)")
    return 0


def cmd_show(args, cfg, gateway):
    doc = store.load_json(cfg, "plan.json")
    if not doc:
        raise SystemExit("no trader/plan.json: run exit.py plan --write")
    spec = gateway.symbol(doc["symbol"])
    doc["_formatted"] = {"entry": format_price(doc["entry"], spec),
                         "stop": format_price(doc["stop"], spec),
                         "target": format_price(doc["target"], spec)}
    cli.emit(doc, args, json.dumps(doc, indent=2, default=str))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="FX exit planner")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("plan", help="build trader/plan.json")
    cli.add_common(pl)
    pl.add_argument("--symbol", default=None)
    pl.add_argument("--side", choices=("buy", "sell"), default=None)
    pl.add_argument("--entry", type=float, default=None)
    pl.add_argument("--stop", default="or-mid", help="or-mid, or-low, or-high, price:X, pips:X, pct:X, atr:K")
    pl.add_argument("--target", default="1R", help="1R, 1.5R, 2R ... or price:X")
    pl.add_argument("--or-high", type=float, default=None)
    pl.add_argument("--or-low", type=float, default=None)
    pl.add_argument("--session", default=None)
    pl.add_argument("--setup", default=None)
    pl.add_argument("--flat-by", default=None, help="New York time, e.g. 16:55")
    pl.add_argument("--from-entry", action="store_true")
    pl.add_argument("--from-scan", action="store_true")
    pl.add_argument("--write", action="store_true")

    sh = sub.add_parser("show", help="print trader/plan.json")
    cli.add_common(sh, now=False)

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        if args.cmd == "plan":
            return cmd_plan(args, cfg, gateway, now)
        return cmd_show(args, cfg, gateway)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
