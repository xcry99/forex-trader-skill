#!/usr/bin/env python3
"""Entry timer: watch a session's opening range bar by bar.

    python entry.py check EURUSD --side auto --setup orb --session london
    python entry.py scan --write
    python entry.py watch EURUSD --every 30

The rule is the original one: wait for the opening range to finish, then take
the first *completed* M5 close beyond it. Long above the range high, short
below the range low. ``--setup level`` uses a price you name instead of the
range.
"""

import argparse
import datetime
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, indicators, store, timeutil
from fx.symbols import format_pips, format_price


def _session_day(now, session):
    day = timeutil.session_day(now, session)
    start, end = timeutil.session_window(day, session)
    if now < start:
        day -= datetime.timedelta(days=1)
        start, end = timeutil.session_window(day, session)
    return day, start, end


def evaluate(gateway, spec, now, setup="orb", side="auto", session="london",
             or_minutes=30, confirm="close", level=None, day=None):
    """Return the entry state for one symbol."""
    day, start, end = _session_day(now, session) if day is None else (
        day, *timeutil.session_window(day, session))
    out = {"symbol": spec.name, "setup": setup, "side": None, "status": "waiting",
           "session": session, "day": day.isoformat(), "as_of": timeutil.iso(now),
           "or_minutes": or_minutes, "digits": spec.digits}
    if now >= end:
        out["status"] = "expired"
        out["reason"] = "the %s session ended at %s New York" % (session, timeutil.to_ny(end).strftime("%H:%M"))
        return out
    if now < start:
        out["reason"] = "the %s session opens at %s New York" % (session, timeutil.to_ny(start).strftime("%H:%M"))
        return out

    bars = gateway.rates(spec.name, "M5", 2500)
    if setup == "level":
        if level is None:
            out["status"] = "error"
            out["reason"] = "a level setup needs --level"
            return out
        level = float(level)
        sb = indicators.session_bars(bars, start, end, now, 5)
        br = indicators.first_break(sb, level, level, side=side, confirm=confirm)
        out.update({"level": spec.round_price(level)})
        if br:
            out.update({"status": "triggered", "side": br["side"], "price": spec.round_price(br["price"]),
                        "at": timeutil.iso(br["at"]), "bar": timeutil.iso(br["bar"]["t_utc"]),
                        "reason": "the %s bar closed %s %s" % (
                            timeutil.clock_label(br["bar"]["t_utc"]),
                            "above" if br["side"] == "buy" else "below",
                            format_price(level, spec))})
        else:
            out["reason"] = "no completed M5 close beyond %s yet" % format_price(level, spec)
        return out

    rng = indicators.opening_range(bars, start, or_minutes, now, 5, rest_end=end)
    if not rng:
        first = start + datetime.timedelta(minutes=or_minutes + 5)
        out["reason"] = ("the %d-minute range is still forming; the first bar after it closes at %s New York"
                         % (or_minutes, timeutil.to_ny(first).strftime("%H:%M")))
        return out
    out.update({"or_high": spec.round_price(rng["high"]), "or_low": spec.round_price(rng["low"]),
                "or_mid": spec.round_price(rng["mid"]), "range_end": timeutil.iso(rng["end"])})
    br = indicators.first_break(rng["rest"], rng["high"], rng["low"], side=side, confirm=confirm)
    if br:
        out.update({"status": "triggered", "side": br["side"], "price": spec.round_price(br["price"]),
                    "at": timeutil.iso(br["at"]), "bar": timeutil.iso(br["bar"]["t_utc"]),
                    "level": spec.round_price(br["level"]),
                    "pct_beyond": round((br["price"] / br["level"] - 1.0) * 100.0, 3),
                    "reason": "the %s bar closed %s the %s" % (
                        timeutil.clock_label(br["bar"]["t_utc"]),
                        "above" if br["side"] == "buy" else "below",
                        "range high" if br["side"] == "buy" else "range low")})
    else:
        out["reason"] = "price is still inside the %d-minute range" % or_minutes
    return out


def _text(e):
    head = "%s %s: %s" % (e["symbol"], e.get("setup"), e["status"].upper())
    lines = [head]
    if e.get("side"):
        lines.append("  side      %s" % e["side"].upper())
    if e.get("price"):
        lines.append("  entry     %s" % e["price"])
    if e.get("or_high") is not None:
        lines.append("  range     %s - %s (mid %s)" % (e["or_high"], e["or_low"], e["or_mid"]))
    if e.get("level") is not None:
        lines.append("  level     %s" % e["level"])
    if e.get("at"):
        lines.append("  at        %s UTC (%s New York)" % (e["at"], timeutil.clock_label(timeutil.parse_utc(e["at"]))))
    if e.get("reason"):
        lines.append("  %s" % e["reason"])
    return "\n".join(lines)


def cmd_check(args, cfg, gateway, now):
    spec = gateway.symbol(args.symbol)
    strat = (cfg.get("strategy") or {}).get("orb") or {}
    e = evaluate(gateway, spec, now, setup=args.setup,
                 side=args.side, session=args.session or strat.get("session") or "london",
                 or_minutes=args.or_minutes or int(strat.get("or_minutes") or 30),
                 confirm=args.confirm or strat.get("confirm") or "close",
                 level=args.level)
    if args.write:
        store.save_json(cfg, "entry.json", {"as_of": timeutil.iso(now), "entries": [e]})
    cli.emit(e, args, _text(e))
    return 0


def cmd_scan(args, cfg, gateway, now):
    scan = store.load_json(cfg, "scan.json")
    if not scan:
        raise SystemExit("no trader/scan.json: run trader-scanner first (scan.py all)")
    strat = (cfg.get("strategy") or {}).get("orb") or {}
    session = args.session or scan.get("session") or strat.get("session") or "london"
    or_minutes = args.or_minutes or int(scan.get("or_minutes") or strat.get("or_minutes") or 30)
    entries = []
    for h in scan.get("hits") or []:
        try:
            spec = gateway.symbol(h["symbol"])
        except Exception:
            continue
        setup = h.get("setup") or "orb"
        if setup == "orb":
            e = evaluate(gateway, spec, now, setup="orb",
                         side=h.get("side") or "auto", session=session, or_minutes=or_minutes,
                         confirm=strat.get("confirm") or "close")
        else:
            # A daily setup is not an opening-range rule: it is already in
            # force, so the entry is the current market price. This is the
            # same convention trader-broker uses in its autonomous pipeline.
            t = gateway.tick(spec.name)
            side = h.get("side") or "buy"
            e = {"symbol": spec.name, "setup": setup, "side": side, "status": "triggered",
                 "price": spec.round_price(t["ask"] if side == "buy" else t["bid"]),
                 "at": timeutil.iso(now), "session": session,
                 "or_high": h.get("or_high"), "or_low": h.get("or_low"),
                 "digits": spec.digits,
                 "reason": "daily setup: " + ", ".join(h.get("setups") or [])}
        e["setups"] = h.get("setups") or []
        e["score"] = h.get("score")
        e["atr_pips"] = h.get("atr_pips")
        e["scan_side"] = h.get("side")
        entries.append(e)
    triggered = [e for e in entries if e["status"] == "triggered"]
    doc = {"as_of": timeutil.iso(now), "session": session, "or_minutes": or_minutes,
           "entries": triggered, "all": entries,
           "counts": {"scanned": len(entries), "triggered": len(triggered),
                      "waiting": sum(1 for e in entries if e["status"] == "waiting"),
                      "expired": sum(1 for e in entries if e["status"] == "expired")}}
    if args.write:
        store.save_json(cfg, "entry.json", doc)
    if args.json:
        print(__import__("json").dumps(doc, indent=2, default=str))
    else:
        rows = [[e["symbol"], e.get("side") or "", e["status"], e.get("price", ""),
                 e.get("or_high", ""), e.get("or_low", ""), e.get("reason", "")]
                for e in entries]
        print(cli.table(rows, ["SYMBOL", "SIDE", "STATUS", "ENTRY", "OR-H", "OR-L", "WHY"]))
        print("\n%d triggered, %d waiting, %d expired%s" % (
            doc["counts"]["triggered"], doc["counts"]["waiting"], doc["counts"]["expired"],
            "" if args.write else " (not written)"))
    return 0


def cmd_watch(args, cfg, gateway, now):
    spec = gateway.symbol(args.symbol)
    strat = (cfg.get("strategy") or {}).get("orb") or {}
    until = None
    if args.until:
        until = timeutil.parse_now(args.until)
    errors = 0
    last = None
    while True:
        try:
            e = evaluate(gateway, spec, timeutil.utc_now() if not args.now else now,
                         setup=args.setup, side=args.side,
                         session=args.session or strat.get("session") or "london",
                         or_minutes=args.or_minutes or int(strat.get("or_minutes") or 30),
                         confirm=args.confirm or strat.get("confirm") or "close",
                         level=args.level)
            errors = 0
        except Exception as ex:
            errors += 1
            print("error %d/%d: %s" % (errors, args.max_errors, ex))
            if errors >= args.max_errors:
                return 1
            time.sleep(args.every)
            continue
        line = _text(e)
        if line != last:
            print(line)
            print("")
            last = line
        if e["status"] in ("triggered", "expired", "error"):
            if args.write:
                store.save_json(cfg, "entry.json", {"as_of": timeutil.iso(timeutil.utc_now()), "entries": [e]})
            return 0
        if until and timeutil.utc_now() >= until:
            print("stopped: --until reached")
            return 0
        time.sleep(args.every)


def main(argv=None):
    p = argparse.ArgumentParser(description="FX entry timer")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="evaluate one symbol now")
    cli.add_common(c)
    c.add_argument("symbol")
    c.add_argument("--setup", choices=("orb", "level"), default="orb")
    c.add_argument("--side", choices=("auto", "buy", "sell"), default="auto")
    c.add_argument("--session", default=None)
    c.add_argument("--or-minutes", type=int, default=None)
    c.add_argument("--confirm", choices=("close", "wick"), default=None)
    c.add_argument("--level", type=float, default=None)
    c.add_argument("--write", action="store_true")

    s = sub.add_parser("scan", help="evaluate every hit in trader/scan.json")
    cli.add_common(s)
    s.add_argument("--session", default=None)
    s.add_argument("--or-minutes", type=int, default=None)
    s.add_argument("--write", action="store_true")

    w = sub.add_parser("watch", help="poll until the setup triggers")
    cli.add_common(w)
    w.add_argument("symbol")
    w.add_argument("--setup", choices=("orb", "level"), default="orb")
    w.add_argument("--side", choices=("auto", "buy", "sell"), default="auto")
    w.add_argument("--session", default=None)
    w.add_argument("--or-minutes", type=int, default=None)
    w.add_argument("--confirm", choices=("close", "wick"), default=None)
    w.add_argument("--level", type=float, default=None)
    w.add_argument("--every", type=int, default=30)
    w.add_argument("--until", default=None)
    w.add_argument("--max-errors", type=int, default=5)
    w.add_argument("--write", action="store_true")

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        if args.cmd == "check":
            return cmd_check(args, cfg, gateway, now)
        if args.cmd == "scan":
            return cmd_scan(args, cfg, gateway, now)
        return cmd_watch(args, cfg, gateway, now)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
