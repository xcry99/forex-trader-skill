#!/usr/bin/env python3
"""Trade journal: every closed trade, every decision, and the performance.

    python journal.py sync
    python journal.py report
    python journal.py report --json
    python journal.py trades --limit 20
    python journal.py decisions --limit 30
    python journal.py export --csv trader/trades.csv

The journal is built from MetaTrader's own deal history, not from the agent's
memory: if the terminal says the position closed, it is in here. The plan
(setup, risk, Time Machine odds) is attached by position ticket.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, journal, store, timeutil
from fx.symbols import format_pips


def cmd_sync(args, cfg, gateway):
    res = journal.sync(gateway, cfg, store.load_state(cfg, "broker"), days=args.days)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
        return 0
    print("journal: %d deals, %d closed positions, %d new" % (res["fetched"],
                                                              res["closed_positions"], res["added"]))
    for r in res["records"]:
        print("  %s %s %s %.2f lots  %s pips  %s  %sR"
              % ((r.get("closed") or "")[:16], r["symbol"], r["side"].upper(), r["volume"],
                 r["pips"], r["net"], r["r_multiple"]))
    return 0


def _metrics(cfg):
    recs = store.read_jsonl(cfg, "journal.jsonl")
    start = ((cfg.get("account") or {}).get("starting_balance")
             or (cfg.get("journal") or {}).get("starting_balance") or 1000.0)
    return recs, journal.metrics(recs, start)


def cmd_report(args, cfg, gateway):
    recs, m = _metrics(cfg)
    decisions = store.read_jsonl(cfg, "decisions.jsonl")
    open_n = None
    if gateway:
        try:
            open_n = len(gateway.positions())
        except Exception:
            open_n = None
    doc = {"as_of": timeutil.iso(timeutil.utc_now()), "metrics": m,
           "decisions": journal.decision_stats(decisions)}
    if args.write:
        store.save_json(cfg, "performance.json", doc)
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        print(journal.report(m, decisions, open_n))
        if args.write:
            print("\n-> trader/performance.json")
    return 0


def cmd_trades(args, cfg):
    recs = store.read_jsonl(cfg, "journal.jsonl")
    recs.sort(key=lambda r: r.get("closed") or "", reverse=True)
    rows = []
    for r in recs[:args.limit]:
        rows.append([(r.get("closed") or "")[:16], r.get("symbol"), (r.get("side") or "").upper(),
                     "%.2f" % (r.get("volume") or 0), r.get("pips"), r.get("net"),
                     r.get("r_multiple"), r.get("setup") or "", (r.get("comment") or "")[:18]])
    print(cli.table(rows, ["CLOSED", "SYMBOL", "SIDE", "LOTS", "PIPS", "NET", "R", "SETUP", "NOTE"]))
    print("\n%d closed trade(s)" % len(recs))
    return 0


def cmd_decisions(args, cfg):
    recs = store.read_jsonl(cfg, "decisions.jsonl")
    recs.sort(key=lambda r: r.get("at") or "", reverse=True)
    rows = []
    for r in recs[:args.limit]:
        detail = ", ".join(r.get("reasons") or []) if r.get("reasons") else (
            r.get("setup") or r.get("verdict") or "")
        rows.append([(r.get("at") or "")[:16], r.get("type"), r.get("symbol") or "",
                     (r.get("side") or "").upper(), detail[:80]])
    print(cli.table(rows, ["AT", "TYPE", "SYMBOL", "SIDE", "DETAIL"]))
    print("\n%d decision(s)" % len(recs))
    return 0


def cmd_export(args, cfg):
    recs = store.read_jsonl(cfg, "journal.jsonl")
    out = args.csv or store.path(cfg, "trades.csv")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(journal.to_csv(recs))
    print("wrote %d trade(s) -> %s" % (len(recs), out))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="FX trade journal")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="pull closed deals from MetaTrader")
    cli.add_common(s, now=False)
    s.add_argument("--days", type=int, default=365)

    r = sub.add_parser("report", help="performance metrics")
    cli.add_common(r, now=False)
    r.add_argument("--write", action="store_true")

    t = sub.add_parser("trades", help="list closed trades")
    cli.add_common(t, now=False)
    t.add_argument("--limit", type=int, default=20)

    d = sub.add_parser("decisions", help="the agent's decision log")
    cli.add_common(d, now=False)
    d.add_argument("--limit", type=int, default=30)

    e = sub.add_parser("export", help="write the trades as CSV")
    cli.add_common(e, now=False)
    e.add_argument("--csv", default=None)

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    if args.cmd == "trades":
        return cmd_trades(args, cfg)
    if args.cmd == "decisions":
        return cmd_decisions(args, cfg)
    if args.cmd == "export":
        return cmd_export(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        if args.cmd == "sync":
            return cmd_sync(args, cfg, gateway)
        return cmd_report(args, cfg, gateway)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
