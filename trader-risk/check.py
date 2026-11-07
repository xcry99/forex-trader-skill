#!/usr/bin/env python3
"""Pre-trade risk check.

    python check.py
    python check.py --orders trader/orders.json --json
    python check.py --no-write

PASS or BLOCK per order, with the reason. The verdicts land in
trader/state/risk.json with a fingerprint of each order, so trader-broker only
executes an order that passed *this* check today.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, risk, store, timeutil


def cmd_check(args, cfg, gateway, now):
    if args.orders:
        if os.path.exists(args.orders):
            with open(args.orders, encoding="utf-8") as fh:
                orders = json.load(fh)
        else:
            orders = store.load_json(cfg, args.orders)
    else:
        orders = store.load_json(cfg, "orders.json", []) or []
    if isinstance(orders, dict):
        orders = orders.get("orders") or []
    state = store.load_state(cfg, "broker")
    doc = risk.check(orders, cfg, gateway, state=state, now=now)
    if args.no_write:
        doc["_written"] = False
    else:
        store.save_state(cfg, "risk", doc)
        doc["_written"] = True
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        print(risk.report(doc))
        if not args.no_write:
            print("\n-> trader/state/risk.json")
    return 0 if doc["block"] == 0 else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="FX pre-trade risk check")
    cli.add_common(p)
    p.add_argument("--orders", default=None, help="orders file (default trader/orders.json)")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        return cmd_check(args, cfg, gateway, now)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
