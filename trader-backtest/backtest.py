#!/usr/bin/env python3
"""Walk-forward backtest of the live Forex rules on MetaTrader 5 history.

    python backtest.py run --days 90
    python backtest.py run --days 180 --symbols EURUSD,GBPUSD,USDJPY
    python backtest.py run --days 90 --session london --spread-pips 1.6 --write
    python backtest.py report

It replays the same daily setups, opening-range breakouts, stop/target maths,
lot sizing and entry window the autonomous broker uses, then reports the
metrics the journal reports for live trades. Nothing is sent to the broker:
this command only reads history.

The defaults match trader/config.json (a $1,000 budget, $10 of risk per trade,
$30 a day, three trades a day, London opening range). Override them to study a
different size, but remember that the result then describes that size, not the
configured experiment.
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import backtest, cli, journal, store, timeutil


def _symbols(text):
    return [s.strip().upper() for s in str(text).split(",") if s.strip()]


def cmd_run(args, cfg, gateway, now):
    symbols = _symbols(args.symbols) if args.symbols else None
    try:
        doc = backtest.run(
            cfg, gateway, symbols=symbols, days=args.days, session=args.session,
            or_minutes=args.or_minutes, now=now, spread_pips=args.spread_pips,
            slippage_pips=args.slippage_pips, budget=args.budget,
            risk_usd=args.risk_usd, daily_loss_usd=args.daily_loss,
            max_trades=args.max_trades,
            daily=not args.no_daily, orb=not args.no_orb,
            confirm=args.confirm)
    except backtest.BacktestError as e:
        raise SystemExit(str(e))

    if args.write:
        store.save_json(cfg, "backtest.json", doc)
        csv_path = store.path(cfg, "backtest_trades.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as fh:
            fh.write(journal.to_csv(doc["trades"]))
            fh.write("\n")

    if args.json:
        print(__import__("json").dumps(doc, indent=2, ensure_ascii=False, default=str))
    else:
        print(backtest.report(doc))
        if doc["trades"]:
            print("")
            print("Last trades:")
            print(cli.table(backtest.trades_table(doc["trades"], args.trades),
                            ["CLOSED", "SYMBOL", "SIDE", "SETUP", "LOTS", "ENTRY",
                             "EXIT", "PIPS", "NET", "WHY"]))
        if args.write:
            print("\n-> trader/backtest.json")
            print("-> trader/backtest_trades.csv")
    return 0


def cmd_report(args, cfg):
    doc = store.load_json(cfg, "backtest.json")
    if not doc:
        raise SystemExit("no trader/backtest.json: run backtest.py run --write")
    if args.json:
        print(__import__("json").dumps(doc, indent=2, ensure_ascii=False, default=str))
        return 0
    print(backtest.report(doc))
    if doc["trades"]:
        print("")
        print(cli.table(backtest.trades_table(doc["trades"], args.trades),
                        ["CLOSED", "SYMBOL", "SIDE", "SETUP", "LOTS", "ENTRY",
                         "EXIT", "PIPS", "NET", "WHY"]))
    return 0


VARIANT_LIST = (
    ("base daily+orb london", {"daily": True, "orb": True, "session": "london"}),
    ("daily only", {"daily": True, "orb": False, "session": "london"}),
    ("orb only london", {"daily": False, "orb": True, "session": "london"}),
    ("orb only asia", {"daily": False, "orb": True, "session": "asia"}),
    ("orb only overlap", {"daily": False, "orb": True, "session": "overlap"}),
    ("orb only newyork", {"daily": False, "orb": True, "session": "newyork"}),
)


def cmd_compare(args, cfg, gateway, now):
    """One history load, several variants: which part carries the result?"""
    import datetime

    symbols = _symbols(args.symbols) if args.symbols else [
        str(s).upper() for s in ((cfg.get("strategy") or {}).get("watchlist") or [])]
    if not symbols:
        raise SystemExit("no symbols: give --symbols or set strategy.watchlist")
    start = now - datetime.timedelta(days=int(args.days))
    hist, errors = backtest.load_history(gateway, symbols, start, now)
    if not hist:
        raise SystemExit("no history for %s: %s" % (", ".join(symbols), errors))

    rows, docs = [], []
    for label, kw in VARIANT_LIST:
        try:
            doc = backtest.run(
                cfg, gateway, symbols=symbols, days=args.days, now=now,
                spread_pips=args.spread_pips, slippage_pips=args.slippage_pips,
                budget=args.budget, risk_usd=args.risk_usd,
                daily_loss_usd=args.daily_loss, max_trades=args.max_trades,
                confirm=args.confirm, hist=hist, **kw)
        except backtest.BacktestError as e:
            docs.append({"variant": label, "error": str(e)})
            rows.append([label, "-", "-", "-", "-", "-", "-", "-"])
            continue
        m = doc["summary"]
        rows.append([label, m["trades"], "%.1f%%" % m["win_rate"], "%.2f" % m["net"],
                     "%.2f" % m["profit_factor"], "%.3f" % m["avg_r"],
                     "%.2f" % m["max_drawdown"], m["skipped"]])
        docs.append({"variant": label, "summary": m, "errors": doc["errors"]})

    if args.json:
        print(json.dumps({"days": args.days, "symbols": symbols,
                          "period": {"from": start.date().isoformat(),
                                     "to": now.date().isoformat()},
                          "variants": docs}, indent=2, ensure_ascii=False, default=str))
        return 0

    print("BACKTEST COMPARE  %s -> %s, %d calendar days, %d symbols"
          % (start.date(), now.date(), args.days, len(hist)))
    print(cli.table(rows, ["VARIANT", "TRADES", "WIN", "NET", "PF", "AVG R",
                           "MAX DD", "SKIP"]))
    print("")
    print("Same bars, same sizing, same risk caps. Only the setup/session changes.")
    print("A negative net with a PF near 1 is the spread; a tiny trade count is noise.")
    if errors:
        print("No history: %s" % ", ".join(sorted(errors)))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="FX walk-forward backtest (MetaTrader 5 history)")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="replay the last N days")
    cli.add_common(r)
    r.add_argument("--days", type=int, default=90, help="calendar days to replay (default 90)")
    r.add_argument("--symbols", default=None, help="EURUSD,GBPUSD,... (default strategy.watchlist)")
    r.add_argument("--session", default=None, help="asia, london, overlap, newyork")
    r.add_argument("--or-minutes", type=int, default=None)
    r.add_argument("--confirm", choices=("close", "wick"), default=None)
    r.add_argument("--spread-pips", type=float, default=None,
                   help="force one spread; default uses each M5 bar's spread")
    r.add_argument("--slippage-pips", type=float, default=0.0,
                   help="adverse fill on entries and stop exits (default 0)")
    r.add_argument("--budget", type=float, default=None, help="sizing budget (default account.budget)")
    r.add_argument("--risk-usd", type=float, default=None, help="override max loss per trade")
    r.add_argument("--daily-loss", type=float, default=None, help="override max daily loss")
    r.add_argument("--max-trades", type=int, default=None, help="override trades per day")
    r.add_argument("--no-daily", action="store_true", help="opening-range only")
    r.add_argument("--no-orb", action="store_true", help="daily setups only")
    r.add_argument("--trades", type=int, default=15, help="trades to print (default 15)")
    r.add_argument("--write", action="store_true", help="write trader/backtest.json + CSV")

    d = sub.add_parser("report", help="print the last written backtest")
    cli.add_common(d, now=False)
    d.add_argument("--trades", type=int, default=15)

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    if args.cmd == "report":
        return cmd_report(args, cfg)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        return cmd_run(args, cfg, gateway, now)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
