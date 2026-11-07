#!/usr/bin/env python3
"""Position sizer: turn a plan into lots.

    python size.py --write
    python size.py --budget 1000 --plan trader/plan.json --write
    python size.py limits

Every limit from the original budget manager is here, in FX units: max loss
per trade, margin cap, max order, and the free-margin buffer. The smallest
one binds.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, sizing, store, timeutil
from fx.symbols import format_pips, format_price, money


def cmd_size(args, cfg, gateway, now):
    plan = None
    if args.plan:
        if os.path.exists(args.plan):
            with open(args.plan, encoding="utf-8") as fh:
                plan = json.load(fh)
        else:
            plan = store.load_json(cfg, args.plan)
    else:
        plan = store.load_json(cfg, "plan.json")
    if not plan:
        raise SystemExit("no trader/plan.json: run trader-exit first, or pass --plan")
    spec = gateway.symbol(plan["symbol"])
    tick = gateway.tick(spec.name)
    account = gateway.account()
    budget = args.budget if args.budget is not None else None
    try:
        s = sizing.size_plan(plan, cfg, spec, gateway, account=account, budget=budget,
                             tick=tick, order_type=args.order_type)
    except sizing.SizeError as e:
        raise SystemExit(str(e))
    order = sizing.bracket_order(s)
    doc = {"as_of": timeutil.iso(now), "order": order, "sizing": s,
           "refusal": s.get("refusal")}
    if args.write:
        store.save_json(cfg, "orders.json", [order] if order else [])
        store.save_json(cfg, "state/sizing.json", doc)
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
        return 0

    if not order:
        print("%s %s: NO ORDER" % (s["side"].upper(), s["symbol"]))
        print("  " + str(s.get("refusal")))
        return 1
    print("%s %s  %.2f lots" % (s["side"].upper(), s["symbol"], s["volume"]))
    print("  entry    %s  (%s)" % (format_price(s["entry"], spec), s["entry_from"]))
    print("  stop     %s  (%.1f pips)" % (format_price(s["stop"], spec), s["risk_pips"]))
    print("  target   %s  (%.1f pips, %.2fR)" % (format_price(s["target"], spec),
                                                   s["reward_pips"], s["reward_risk"]))
    print("  risk     %s at the stop, %s per pip per lot" % (money(s["max_loss"]),
                                                              money(s["pip_value_per_lot"])))
    print("  margin   %s at 1:%s" % (money(s["margin"]), int((cfg.get("account") or {}).get("leverage") or 1000)))
    print("  budget   %s, buffer %s, available %s" % (money(s["budget"]), money(s["cash_buffer"]),
                                                       money(s["cash_available"])))
    print("  bound by %s" % (s["bound_by"] or "the plan"))
    print("")
    print("  limits:")
    for l in s["limits"]:
        print("    %-12s %8.2f lots  %s%s" % (l["name"], l["lots"], l["how"],
                                               "  <- binds" if l.get("binds") else ""))
    print("")
    print("  -> trader/orders.json" if args.write else "  (not written: add --write)")
    return 0


def cmd_limits(args, cfg):
    r = cfg.get("risk") or {}
    a = cfg.get("account") or {}
    doc = {"account": a, "risk": r}
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
        return 0
    print("ACCOUNT")
    print("  budget            %s" % money(a.get("budget")))
    print("  leverage          1:%s" % a.get("leverage"))
    print("  mode              %s" % a.get("mode"))
    print("RISK (translated from the stock pack)")
    print("  max loss / trade  %s" % money(r.get("max_loss_per_trade_usd")))
    print("  max daily loss    %s" % money(r.get("max_daily_loss_usd")))
    print("  max trades / day  %s" % r.get("max_trades_per_day"))
    print("  max orders / day  %s" % r.get("max_orders_per_day"))
    print("  min reward:risk   %s" % r.get("min_reward_risk"))
    print("  margin cap        %s%% of equity" % r.get("max_margin_pct"))
    print("  max order         %s lots" % r.get("max_order_lots"))
    print("  cash buffer       %s%%" % r.get("cash_buffer_pct"))
    print("  entry window      %s - %s New York" % (r.get("no_entries_before"), r.get("no_entries_after")))
    print("  flat by           %s New York" % r.get("flat_by"))
    print("  shorts            %s" % ("on" if r.get("allow_short", True) else "off"))
    print("  margin            %s" % ("on" if r.get("allow_margin", True) else "off"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="FX position sizer")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("size", help="size trader/plan.json into trader/orders.json")
    cli.add_common(s)
    s.add_argument("--plan", default=None)
    s.add_argument("--budget", type=float, default=None)
    s.add_argument("--order-type", choices=("market", "limit"), default="market")
    s.add_argument("--write", action="store_true")

    l = sub.add_parser("limits", help="print the account and risk settings")
    cli.add_common(l, now=False)

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    if args.cmd == "limits":
        return cmd_limits(args, cfg)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        return cmd_size(args, cfg, gateway, now)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
