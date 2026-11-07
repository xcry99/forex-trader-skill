#!/usr/bin/env python3
"""Price alerts for FX, including the plan's stop, target and flat-by time.

    python alerts.py add --from-plan
    python alerts.py add --symbol EURUSD --type price --price 1.1000 --direction below
    python alerts.py check
    python alerts.py check --execute        # also close the position on a time/stop/target hit
    python alerts.py list
    python alerts.py quote EURUSD

MetaTrader holds the bracket server-side, so these alerts are a second pair of
eyes (and the thing that flattens a position at the session's flat_by time).
"""

import argparse
import datetime
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, store, timeutil
from fx.symbols import format_pips, format_price


def _rules(cfg):
    doc = store.load_json(cfg, "alerts.json", {"rules": []}) or {"rules": []}
    return doc


def _save(cfg, doc):
    store.save_json(cfg, "alerts.json", doc)


def _state(cfg):
    return store.load_state(cfg, "alerts") or {"fired": []}


def _save_state(cfg, st):
    store.save_state(cfg, "alerts", st)


def rules_from_plan(plan):
    sym = plan["symbol"]
    side = plan["side"]
    date = plan.get("date") or timeutil.to_ny(timeutil.utc_now()).date().isoformat()
    out = [
        {"id": "%s-%s-stop" % (sym, date), "symbol": sym, "type": "stop", "price": plan["stop"],
         "side": side, "note": "plan stop: %s" % plan.get("stop_label", ""), "date": date},
        {"id": "%s-%s-target" % (sym, date), "symbol": sym, "type": "target", "price": plan["target"],
         "side": side, "note": "plan target: %s" % plan.get("target_label", ""), "date": date},
        {"id": "%s-%s-flat" % (sym, date), "symbol": sym, "type": "time",
         "at": plan.get("flat_by") or "16:55", "note": "flat by the session close", "date": date},
    ]
    return out


def cmd_add(args, cfg):
    doc = _rules(cfg)
    new = []
    if args.from_plan:
        plan = store.load_json(cfg, "plan.json")
        if not plan:
            raise SystemExit("no trader/plan.json: run trader-exit first")
        new = rules_from_plan(plan)
    else:
        if not args.symbol or not args.type:
            raise SystemExit("give --symbol and --type, or --from-plan")
        rule = {"id": args.id or "%s-%s-%s" % (args.symbol.upper(), args.type, timeutil.iso(timeutil.utc_now())),
                "symbol": args.symbol.upper(), "type": args.type, "note": args.note or "",
                "date": args.date or timeutil.to_ny(timeutil.utc_now()).date().isoformat()}
        if args.type == "price":
            if args.price is None:
                raise SystemExit("a price alert needs --price")
            rule["price"] = float(args.price)
            rule["direction"] = args.direction or "above"
        elif args.type in ("stop", "target"):
            if args.price is None:
                raise SystemExit("a %s alert needs --price" % args.type)
            rule["price"] = float(args.price)
            rule["side"] = args.side or "buy"
        elif args.type == "time":
            if not args.at:
                raise SystemExit("a time alert needs --at HH:MM (New York)")
            rule["at"] = args.at
        elif args.type == "pips_move":
            if args.pips is None:
                raise SystemExit("a pips_move alert needs --pips")
            rule["pips"] = float(args.pips)
            rule["ref_price"] = args.ref_price
        elif args.type == "spread":
            if args.spread is None:
                raise SystemExit("a spread alert needs --spread")
            rule["spread"] = float(args.spread)
        else:
            raise SystemExit("unknown alert type %r" % args.type)
        new = [rule]
    ids = {r.get("id") for r in doc.get("rules", [])}
    for r in new:
        if r["id"] not in ids:
            doc.setdefault("rules", []).append(r)
            print("added %s: %s %s" % (r["id"], r["type"], r.get("price", r.get("at", ""))))
        else:
            print("already there: %s" % r["id"])
    _save(cfg, doc)
    return 0


def _active(rule, now):
    d = rule.get("date")
    if d and d != timeutil.to_ny(now).date().isoformat():
        return False
    return True


def evaluate(rule, quotes, now):
    """(ring?, message) for one rule."""
    if not _active(rule, now):
        return False, None
    typ = rule.get("type")
    sym = rule.get("symbol")
    q = quotes.get(sym) if sym else None
    if typ in ("price", "stop", "target") and q:
        px = q["bid"]
        price = float(rule["price"])
        if typ == "price":
            if rule.get("direction") == "below":
                return px <= price, "%s %.5f <= %.5f" % (sym, px, price)
            return px >= price, "%s %.5f >= %.5f" % (sym, px, price)
        side = rule.get("side") or "buy"
        if typ == "stop":
            if side == "buy":
                return q["bid"] <= price, "%s bid %.5f hit the stop %.5f" % (sym, q["bid"], price)
            return q["ask"] >= price, "%s ask %.5f hit the stop %.5f" % (sym, q["ask"], price)
        if side == "buy":
            return q["bid"] >= price, "%s bid %.5f reached the target %.5f" % (sym, q["bid"], price)
        return q["ask"] <= price, "%s ask %.5f reached the target %.5f" % (sym, q["ask"], price)
    if typ == "time":
        try:
            h, m = [int(x) for x in str(rule["at"]).split(":")]
        except (ValueError, KeyError):
            return False, None
        target = timeutil.flat_by_utc(timeutil.to_ny(now).date(), "%02d:%02d" % (h, m))
        return now >= target, "it is %s New York (alert at %s)" % (timeutil.clock_label(now), rule["at"])
    if typ == "pips_move" and q:
        ref = float(rule.get("ref_price") or q["mid"])
        spec_pips = abs(q["mid"] - ref) / q["pip_size"]
        return spec_pips >= float(rule["pips"]), "%s moved %.1f pips from %.5f" % (sym, spec_pips, ref)
    if typ == "spread" and q:
        return q["spread_pips"] >= float(rule["spread"]), "%s spread %.1f pips" % (sym, q["spread_pips"])
    return False, None


def cmd_check(args, cfg, gateway, now):
    doc = _rules(cfg)
    rules = doc.get("rules") or []
    if not rules:
        print("no alerts: add some with alerts.py add --from-plan")
        return 0
    symbols = sorted({r.get("symbol") for r in rules if r.get("symbol")})
    quotes = {}
    for s in symbols:
        try:
            quotes[s] = gateway.tick(s)
        except Exception as e:
            print("no quote for %s: %s" % (s, e))
    st = _state(cfg)
    fired = set(st.get("fired") or [])
    rang = 0
    for r in rules:
        if r.get("id") in fired:
            continue
        hit, msg = evaluate(r, quotes, now)
        if not hit:
            continue
        rang += 1
        fired.add(r.get("id"))
        print("ALERT  %s  %s" % (r.get("id"), msg or r.get("note") or ""))
        if args.execute and r.get("symbol"):
            ps = [p for p in gateway.positions() if p["symbol"] == gateway.resolve(r["symbol"])]
            for p in ps:
                try:
                    gateway.close_position(p, comment=("alert-" + str(r.get("type")))[:31])
                    print("  closed %s %s %.2f lots" % (p["symbol"], p["side"], p["volume"]))
                except Exception as e:
                    print("  could not close %s: %s" % (p["ticket"], e))
    st["fired"] = sorted(fired)
    st["checked_at"] = timeutil.iso(now)
    _save_state(cfg, st)
    if not rang:
        print("nothing rang (%d rule(s) armed)" % len(rules))
    return 0


def cmd_list(args, cfg):
    doc = _rules(cfg)
    rows = [[r.get("id"), r.get("symbol") or "", r.get("type"), r.get("price", r.get("at", r.get("pips", ""))),
             r.get("direction") or r.get("side") or "", r.get("note") or "", r.get("date") or ""]
            for r in doc.get("rules") or []]
    print(cli.table(rows, ["ID", "SYMBOL", "TYPE", "LEVEL", "SIDE", "NOTE", "DATE"]))
    return 0


def cmd_reset(args, cfg):
    st = _state(cfg)
    if args.id:
        st["fired"] = [x for x in st.get("fired") or [] if x != args.id]
    else:
        st["fired"] = []
    _save_state(cfg, st)
    print("alerts re-armed")
    return 0


def cmd_quote(args, cfg, gateway):
    rows = []
    for s in args.symbols:
        q = gateway.tick(s)
        spec = gateway.symbol(q["symbol"])
        rows.append([q["symbol"], format_price(q["bid"], spec), format_price(q["ask"], spec),
                     "%.1f" % q["spread_pips"], timeutil.iso(q["time_utc"])])
    print(cli.table(rows, ["SYMBOL", "BID", "ASK", "SPREAD", "TIME"]))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="FX alerts")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="add an alert")
    cli.add_common(a, now=False)
    a.add_argument("--from-plan", action="store_true")
    a.add_argument("--symbol", default=None)
    a.add_argument("--type", choices=("price", "stop", "target", "time", "pips_move", "spread"), default=None)
    a.add_argument("--price", type=float, default=None)
    a.add_argument("--direction", choices=("above", "below"), default=None)
    a.add_argument("--side", choices=("buy", "sell"), default=None)
    a.add_argument("--at", default=None)
    a.add_argument("--pips", type=float, default=None)
    a.add_argument("--ref-price", type=float, default=None)
    a.add_argument("--spread", type=float, default=None)
    a.add_argument("--note", default=None)
    a.add_argument("--date", default=None)
    a.add_argument("--id", default=None)

    c = sub.add_parser("check", help="ring what hit")
    cli.add_common(c)
    c.add_argument("--execute", action="store_true")

    l = sub.add_parser("list", help="list the alerts")
    cli.add_common(l, now=False)

    r = sub.add_parser("reset", help="re-arm fired alerts")
    cli.add_common(r, now=False)
    r.add_argument("--id", default=None)

    q = sub.add_parser("quote", help="print a live quote")
    cli.add_common(q, now=False)
    q.add_argument("symbols", nargs="+")

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    if args.cmd == "add":
        return cmd_add(args, cfg)
    if args.cmd == "list":
        return cmd_list(args, cfg)
    if args.cmd == "reset":
        return cmd_reset(args, cfg)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        if args.cmd == "quote":
            return cmd_quote(args, cfg, gateway)
        return cmd_check(args, cfg, gateway, now)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
