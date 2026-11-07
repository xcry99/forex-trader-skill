#!/usr/bin/env python3
"""Autonomous FX broker for MetaTrader 5.

    python broker.py account
    python broker.py positions
    python broker.py show
    python broker.py execute            # send the orders that passed trader-risk
    python broker.py auto --once        # scan -> news -> entry -> exit -> timemachine
                                        # -> size -> risk -> execute, no prompts
    python broker.py auto --loop --interval 300
    python broker.py close EURUSD
    python broker.py close --all
    python broker.py flatten            # close everything at flat_by
    python broker.py sync               # pull closed trades into the journal

There is no per-order confirmation. The risk check is still the gate: an
order is only sent when trader-risk wrote a PASS for that exact fingerprint
today. On a live account ``--allow-live`` is required; the demo account this
pack is built for needs nothing.
"""

import argparse
import datetime
import importlib.util
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, indicators, journal, plan as plan_mod, risk as risk_mod, sizing
from fx import store, timeutil
from fx.symbols import format_pips, format_price, money


# ------------------------------------------------------------------ helpers

def _load(name, rel):
    path = os.path.join(ROOT, rel)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _decision(cfg, kind, **fields):
    rec = {"at": timeutil.iso(timeutil.utc_now()), "type": kind}
    rec.update(fields)
    store.append_jsonl(cfg, "decisions.jsonl", rec)
    return rec


def _magic(cfg):
    return int((cfg.get("mt5") or {}).get("magic") or 20261107)


def _today(now):
    return timeutil.to_ny(now).date().isoformat()


def _flat_by_utc(cfg, now):
    fb = (cfg.get("risk") or {}).get("flat_by") or "16:55"
    return timeutil.flat_by_utc(timeutil.to_ny(now).date(), fb)


def _pips(gateway, pos):
    spec = gateway.symbol(pos["symbol"])
    delta = (pos.get("current_price") or pos["price_open"]) - pos["price_open"]
    if pos["side"] == "sell":
        delta = -delta
    return spec.pips(delta)


def _print_account(gateway):
    a = gateway.account_summary()
    print("ACCOUNT %s  %s  (%s)" % (a["login"], a["server"], a["trade_mode"]))
    print("  balance %s   equity %s   margin %s   free %s"
          % (money(a["balance"], a["currency"]), money(a["equity"], a["currency"]),
             money(a["margin"], a["currency"]), money(a["margin_free"], a["currency"])))
    print("  leverage 1:%s   open positions %d   floating %s"
          % (a["leverage"], a["open_positions"], money(a["profit"], a["currency"])))
    return a


def _print_positions(gateway, positions=None):
    positions = positions if positions is not None else gateway.positions()
    if not positions:
        print("no open positions")
        return []
    rows = []
    for p in positions:
        spec = gateway.symbol(p["symbol"])
        rows.append([p["ticket"], p["symbol"], p["side"].upper(), "%.2f" % p["volume"],
                     format_price(p["price_open"], spec),
                     format_price(p.get("current_price") or 0, spec),
                     format_price(p["sl"], spec) if p.get("sl") else "-",
                     format_price(p["tp"], spec) if p.get("tp") else "-",
                     "%.1f" % _pips(gateway, p), "%.2f" % p.get("profit", 0)])
    print(cli.table(rows, ["TICKET", "SYMBOL", "SIDE", "LOTS", "OPEN", "NOW", "SL", "TP", "PIPS", "P/L"]))
    return positions


def _positions_ours(gateway, cfg, all_positions=False):
    ps = gateway.positions()
    if all_positions:
        return ps
    magic = _magic(cfg)
    return [p for p in ps if int(p.get("magic") or 0) == magic]


# ------------------------------------------------------------------ risk gate

def _risk_state(cfg):
    return store.load_state(cfg, "risk") or {}


def _ready(cfg, order, now):
    st = _risk_state(cfg)
    if not st or st.get("today") != _today(now):
        return False, "no risk verdict for today: run trader-risk"
    fp = risk_mod.fingerprint(order)
    for r in st.get("results") or []:
        if r.get("fingerprint") == fp:
            if r.get("verdict") == "PASS":
                return True, "PASS"
            return False, "; ".join(r.get("reasons") or ["BLOCK"])
    return False, "this exact order has no risk verdict: run trader-risk again"


def _live_guard(cfg, gateway, allow_live):
    a = gateway.account()
    if not a.get("is_demo") and not allow_live:
        raise SystemExit("this is a LIVE account. Re-run with --allow-live if you really mean it.")
    return a


# ------------------------------------------------------------------ execution

def _send_order(cfg, gateway, order, now):
    spec = gateway.symbol(order["symbol"])
    tick = gateway.tick(spec.name)
    side = order["side"]
    # Re-check the plan against the live price: do not chase a move that has
    # already reached the target or the stop.
    if side == "buy":
        if tick["ask"] >= order["tp"]:
            return None, "target already reached (ask %s >= tp %s)" % (tick["ask"], order["tp"])
        if tick["ask"] <= order["sl"]:
            return None, "stop already hit (ask %s <= sl %s)" % (tick["ask"], order["sl"])
    else:
        if tick["bid"] <= order["tp"]:
            return None, "target already reached (bid %s <= tp %s)" % (tick["bid"], order["tp"])
        if tick["bid"] >= order["sl"]:
            return None, "stop already hit (bid %s >= sl %s)" % (tick["bid"], order["sl"])
    res = gateway.open_order(spec.name, side, order["volume"], sl=order["sl"], tp=order["tp"],
                             comment=order.get("comment") or "forex-trader", magic=_magic(cfg))
    return res, None


def _record_placed(cfg, order, res, now):
    st = store.load_state(cfg, "broker")
    st.setdefault("orders", [])
    rec = dict(order)
    rec.update({
        "status": "placed", "date": _today(now), "placed_at": timeutil.iso(now),
        "position": res.get("position") or res.get("order"),
        "fill_price": res.get("price"), "retcode": res.get("retcode"),
        "fingerprint": risk_mod.fingerprint(order),
        "max_loss": (order.get("plan") or {}).get("max_loss"),
        "kind": "entry",
    })
    st["orders"].append(rec)
    st["updated"] = timeutil.iso(now)
    store.save_state(cfg, "broker", st)
    return rec


def cmd_execute(args, cfg, gateway, now):
    _live_guard(cfg, gateway, args.allow_live)
    orders = store.load_json(cfg, "orders.json", []) or []
    if isinstance(orders, dict):
        orders = orders.get("orders") or []
    if not orders:
        print("no orders in trader/orders.json")
        return 0
    sent = 0
    for o in orders:
        ok, why = _ready(cfg, o, now)
        if not ok:
            print("SKIP  %s %s: %s" % (o.get("side", "").upper(), o.get("symbol"), why))
            _decision(cfg, "blocked", symbol=o.get("symbol"), side=o.get("side"),
                      reasons=[why], stage="broker")
            continue
        if args.dry_run:
            print("DRY   %s %s %.2f lots (risk PASS)" % (o["side"].upper(), o["symbol"], o["volume"]))
            continue
        try:
            res, why = _send_order(cfg, gateway, o, now)
        except Exception as e:
            print("ERROR %s %s: %s" % (o["side"].upper(), o["symbol"], e))
            _decision(cfg, "blocked", symbol=o.get("symbol"), side=o.get("side"),
                      reasons=["broker: %s" % e], stage="broker")
            continue
        if res is None:
            print("SKIP  %s %s: %s" % (o["side"].upper(), o["symbol"], why))
            _decision(cfg, "blocked", symbol=o.get("symbol"), side=o.get("side"),
                      reasons=[why], stage="broker")
            continue
        rec = _record_placed(cfg, o, res, now)
        sent += 1
        print("FILLED %s %s %.2f lots @ %s (ticket %s, SL %s, TP %s)"
              % (o["side"].upper(), o["symbol"], o["volume"], res.get("price"),
                 res.get("position") or res.get("order"), o["sl"], o["tp"]))
        _decision(cfg, "executed", symbol=o.get("symbol"), side=o.get("side"),
                  volume=o["volume"], price=res.get("price"),
                  position=res.get("position") or res.get("order"),
                  setup=(o.get("plan") or {}).get("setup"),
                  risk_usd=(o.get("plan") or {}).get("max_loss"), stage="broker")
    print("\n%d order(s) sent" % sent)
    return 0


# ------------------------------------------------------------------ pipeline

def _merge_hits(daily, orb):
    merged = {}
    for h in daily + orb:
        key = (h["symbol"], h["side"])
        if key in merged:
            merged[key]["setups"] = sorted(set(merged[key]["setups"]) | set(h["setups"]))
            merged[key]["score"] = indicators.setup_score(merged[key]["setups"])
            merged[key]["price"] = h["price"]
            merged[key]["atr_pips"] = h.get("atr_pips") or merged[key].get("atr_pips")
            for k in ("or_high", "or_low", "or_mid", "level", "at", "session", "day"):
                if h.get(k) is not None:
                    merged[key][k] = h[k]
        else:
            merged[key] = dict(h)
    return sorted(merged.values(), key=lambda h: (-h["score"], -(h.get("atr_pips") or 0), h["symbol"]))


def _atr_for(gateway, spec, now, session, timeframe="M5"):
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


def _entry_for_hit(gateway, cfg, hit, now, session, or_minutes, entry_mod):
    spec = gateway.symbol(hit["symbol"])
    if hit.get("setup") == "orb":
        return entry_mod.evaluate(gateway, spec, now, setup="orb", side=hit["side"],
                                  session=session, or_minutes=or_minutes,
                                  confirm=((cfg.get("strategy") or {}).get("orb") or {}).get("confirm", "close"))
    t = gateway.tick(spec.name)
    return {"symbol": spec.name, "setup": hit.get("setup") or "daily", "side": hit["side"],
            "status": "triggered", "price": t["ask"] if hit["side"] == "buy" else t["bid"],
            "at": timeutil.iso(now), "session": session,
            "reason": "daily setup: " + ", ".join(hit.get("setups") or []),
            "setups": hit.get("setups") or [], "score": hit.get("score"),
            "atr_pips": hit.get("atr_pips")}


def _build_plan(gateway, cfg, entry, hit, now, session):
    spec = gateway.symbol(entry["symbol"])
    side = entry["side"]
    entry_px = float(entry["price"])
    setup = entry.get("setup") or "orb"
    if setup == "orb":
        stop_rule, stop_param = "or_mid", None
        or_high, or_low = entry.get("or_high"), entry.get("or_low")
        atr, atr_n = _atr_for(gateway, spec, now, session)
    else:
        stop_rule, stop_param = "atr", 1.5
        or_high, or_low = hit.get("or_high"), hit.get("or_low")
        atr, atr_n = _atr_for(gateway, spec, now, session, timeframe="D1")
    try:
        p = plan_mod.compute(entry_px, side, stop_rule, stop_param, "r", 1.0, spec,
                             or_high=or_high, or_low=or_low, atr=atr, atr_bars=atr_n)
    except plan_mod.PlanError:
        # A range stop can be too wide for a 1:1 target; fall back to ATR.
        if stop_rule == "or_mid":
            p = plan_mod.compute(entry_px, side, "atr", 1.5, "r", 1.0, spec,
                                 atr=atr, atr_bars=atr_n)
            stop_rule = "atr"
        else:
            raise
    doc = {
        "symbol": spec.name, "side": side, "setup": setup,
        "setups": hit.get("setups") or entry.get("setups") or [],
        "session": session, "date": timeutil.to_ny(now).date().isoformat(),
        "entry_time": timeutil.iso(now),
        "flat_by": (cfg.get("risk") or {}).get("flat_by") or "16:55",
        "or_high": spec.round_price(or_high) if or_high is not None else None,
        "or_low": spec.round_price(or_low) if or_low is not None else None,
        "confidence": entry.get("score"),
        **p,
    }
    return doc


def _news_for(spec, doc):
    """The news context for a pair, from the cached news document."""
    if not doc:
        return {"risk": "unknown", "currencies": [], "high": 0, "titles": []}
    ccys = [c for c in (spec.currency_base, spec.currency_profit) if c]
    summary = doc.get("currencies") or {}
    highs = [summary.get(c, {}).get("high", 0) for c in ccys]
    levels = [summary.get(c, {}).get("risk", "low") for c in ccys]
    risk = "high" if "high" in levels else ("medium" if "medium" in levels else "low")
    titles = []
    for c in ccys:
        titles.extend((summary.get(c) or {}).get("titles") or [])
    return {"risk": risk, "currencies": ccys, "high": sum(highs),
            "titles": titles[:4], "as_of": doc.get("as_of")}


def _news_blackout(cfg, doc, spec, now):
    """A high-impact item inside the configured window, or None.

    ``avoid_high_impact_minutes`` is 0 by default, so news is context only.
    A positive value is an explicit user choice to stand aside.
    """
    minutes = float(((cfg.get("strategy") or {}).get("news") or {})
                    .get("avoid_high_impact_minutes") or 0)
    if minutes <= 0 or not doc:
        return None
    cutoff = now - datetime.timedelta(minutes=minutes)
    ccys = {c for c in (spec.currency_base, spec.currency_profit) if c}
    for it in doc.get("items") or []:
        if it.get("impact") != "high":
            continue
        if not (set(it.get("currencies") or []) & ccys):
            continue
        published = timeutil.parse_utc(it.get("published"))
        if published and published >= cutoff:
            return it
    return None


def _timemachine(gateway, cfg, plan_doc, now, tm_mod):
    tmcfg = (cfg.get("strategy") or {}).get("timemachine") or {}
    try:
        tm = tm_mod.compute(gateway, plan_doc, cfg, now,
                            replays=int(tmcfg.get("replays") or 10000),
                            seed=int(tmcfg.get("seed") or 19),
                            window=int(tmcfg.get("window") or 2),
                            days=int(tmcfg.get("days") or 60))
    except Exception as e:
        return {"error": str(e), "verdict": "unavailable", "expectancy_r": None}
    return {"target_first": tm["monte_carlo"]["target_first"],
            "stop_first": tm["monte_carlo"]["stop_first"],
            "open_at_end": tm["monte_carlo"]["open_at_end"],
            "expectancy_r": tm["monte_carlo"]["expectancy_r"],
            "history": tm["history"], "verdict": tm["verdict"],
            "replays": tm["monte_carlo"]["replays"], "seed": tm["seed"]}


def _close_all(cfg, gateway, now, positions=None, reason="flatten", all_positions=False):
    ps = positions if positions is not None else _positions_ours(gateway, cfg, all_positions)
    closed = 0
    for p in ps:
        try:
            res = gateway.close_position(p, comment=reason[:31])
        except Exception as e:
            print("could not close %s %s: %s" % (p["symbol"], p["ticket"], e))
            continue
        closed += 1
        print("CLOSED %s %s %.2f lots @ %s (%s)"
              % (p["symbol"], p["side"].upper(), p["volume"], res.get("price"), reason))
        _decision(cfg, "closed", symbol=p["symbol"], side=p["side"], volume=p["volume"],
                  price=res.get("price"), position=p["ticket"], reason=reason)
        st = store.load_state(cfg, "broker")
        for r in st.get("orders", []):
            if int(r.get("position") or 0) == int(p["ticket"]):
                r["status"] = "closed"
                r["closed_at"] = timeutil.iso(now)
        store.save_state(cfg, "broker", st)
    return closed


def cmd_auto(args, cfg, gateway, now, once=True):
    scan_mod = _load("fx_scan", os.path.join("trader-scanner", "scan.py"))
    entry_mod = _load("fx_entry", os.path.join("trader-entry", "entry.py"))
    tm_mod = _load("fx_tm", os.path.join("trader-timemachine", "timemachine.py"))
    news_mod = _load("fx_news", os.path.join("trader-news", "news.py"))

    _live_guard(cfg, gateway, args.allow_live)
    strat = cfg.get("strategy") or {}
    orbcfg = strat.get("orb") or {}
    session = args.session or orbcfg.get("session") or "london"
    or_minutes = args.or_minutes or int(orbcfg.get("or_minutes") or 30)
    watch = [str(s).upper() for s in (strat.get("watchlist") or [])]

    pinned = getattr(args, "now", None)
    if pinned and getattr(args, "loop", False):
        raise SystemExit("--now pins the clock: use it with --once, not --loop")
    while True:
        now = now if pinned else timeutil.utc_now()
        acct = gateway.account()
        positions = _positions_ours(gateway, cfg)
        print("=" * 70)
        print("CYCLE %s  %s  equity %s  positions %d"
              % (timeutil.iso(now), timeutil.to_ny(now).strftime("%a %H:%M NY"),
                 money(acct.get("equity"), acct.get("currency")), len(positions)))
        _decision(cfg, "cycle", equity=acct.get("equity"), balance=acct.get("balance"),
                  positions=len(positions), session=session)

        # 1. Time exit: the original "flat by the close" rule.
        flat = _flat_by_utc(cfg, now)
        if positions and now >= flat:
            print("flat_by %s New York reached: closing %d position(s)"
                  % ((cfg.get("risk") or {}).get("flat_by"), len(positions)))
            _close_all(cfg, gateway, now, positions, reason="flat_by")
            positions = []
        # 2. Attach missing brackets from the plan, if any.
        for p in positions:
            if not p.get("sl") or not p.get("tp"):
                st = store.load_state(cfg, "broker")
                rec = next((r for r in st.get("orders", [])
                            if int(r.get("position") or 0) == int(p["ticket"])), None)
                if rec:
                    try:
                        gateway.modify_position(p["ticket"], rec["sl"], rec["tp"])
                        print("attached SL/TP to %s ticket %s" % (p["symbol"], p["ticket"]))
                    except Exception as e:
                        print("could not attach SL/TP to %s: %s" % (p["ticket"], e))

        is_open, why = timeutil.market_open(now)
        if not is_open:
            print("market closed: %s" % why)
            _decision(cfg, "skipped", stage="market", reasons=[why])
            journal.sync(gateway, cfg, store.load_state(cfg, "broker"))
            if once:
                return 0
            time.sleep(args.interval)
            continue

        # 3. Scan. News is cached context by default; --news refreshes it.
        news_doc = store.load_json(cfg, "news.json")
        if args.news:
            try:
                ncfg = strat.get("news") or {}
                hours = float(ncfg.get("hours") or 24)
                items, nerr = news_mod.collect(cfg, hours=hours, now=now)
                news_doc = {"as_of": timeutil.iso(now), "hours": hours, "items": items,
                            "currencies": news_mod.summarize(items), "errors": nerr}
                store.save_json(cfg, "news.json", news_doc)
                print("news: %d item(s) from %d feed(s), %d failed"
                      % (len(items), len((cfg.get("news") or {}).get("feeds") or []),
                         len(nerr)))
            except Exception as e:
                print("news: %s (using the cached file)" % e)
        dh, dc, de = scan_mod._daily_hits(cfg, gateway, now, watch)
        oh, oc, oe, day = scan_mod._orb_hits(cfg, gateway, now, watch, session, or_minutes,
                                             orbcfg.get("confirm") or "close")
        hits = _merge_hits(dh, oh)
        print("scan: %d daily, %d opening-range, %d merged candidate(s)"
              % (dc.get("hits", 0), oc.get("hits", 0), len(hits)))
        for h in hits[:12]:
            _decision(cfg, "candidate", symbol=h["symbol"], side=h["side"],
                      setups=h["setups"], score=h["score"], atr_pips=h.get("atr_pips"))

        # 4. Entry + plan + time machine + size, for the best candidates.
        max_c = int(args.max_candidates or strat.get("scan", {}).get("max_candidates") or 6)
        orders, plans = [], []
        for h in hits[:max_c]:
            entry = _entry_for_hit(gateway, cfg, h, now, session, or_minutes, entry_mod)
            if entry.get("status") != "triggered":
                _decision(cfg, "skipped", symbol=h["symbol"], side=h["side"], stage="entry",
                          reasons=[entry.get("reason") or entry.get("status")])
                continue
            try:
                plan_doc = _build_plan(gateway, cfg, entry, h, now, session)
            except Exception as e:
                _decision(cfg, "skipped", symbol=h["symbol"], side=h["side"], stage="exit",
                          reasons=[str(e)])
                continue
            news_spec = gateway.symbol(h["symbol"])
            plan_doc["news"] = _news_for(news_spec, news_doc)
            blackout = _news_blackout(cfg, news_doc, news_spec, now)
            if blackout:
                _decision(cfg, "skipped", symbol=h["symbol"], side=h["side"], stage="news",
                          reasons=["high-impact news: " + (blackout.get("title") or "")])
                continue
            plan_doc["timemachine"] = _timemachine(gateway, cfg, plan_doc, now, tm_mod)
            plan_doc["confidence"] = round(
                (entry.get("score") or 0) + (plan_doc["timemachine"].get("expectancy_r") or 0), 3)
            plans.append(plan_doc)

        # Rank by the Time Machine's expectancy, then by setup score.
        plans.sort(key=lambda p: (-(p.get("timemachine") or {}).get("expectancy_r", -9),
                                  -(p.get("confidence") or 0)))
        for plan_doc in plans:
            spec = gateway.symbol(plan_doc["symbol"])
            try:
                s = sizing.size_plan(plan_doc, cfg, spec, gateway, account=acct,
                                     order_type="market")
            except sizing.SizeError as e:
                _decision(cfg, "skipped", symbol=plan_doc["symbol"], side=plan_doc["side"],
                          stage="size", reasons=[str(e)])
                continue
            order = sizing.bracket_order(s)
            if not order:
                print("no order: %s %s: %s" % (plan_doc["side"].upper(), plan_doc["symbol"],
                                               s.get("refusal")))
                _decision(cfg, "skipped", symbol=plan_doc["symbol"], side=plan_doc["side"],
                          stage="size", reasons=[s.get("refusal")])
                continue
            order["plan"]["timemachine"] = plan_doc.get("timemachine")
            order["plan"]["confidence"] = plan_doc.get("confidence")
            order["plan"]["news"] = plan_doc.get("news")
            orders.append(order)
            tm = plan_doc.get("timemachine") or {}
            print("plan %s %s %.2f lots  risk %s  RR %.2f  TM %+.2fR (%s)"
                  % (order["side"].upper(), order["symbol"], order["volume"],
                     money(s["max_loss"]), s["reward_risk"], tm.get("expectancy_r") or 0,
                     tm.get("verdict")))

        if not orders:
            print("no tradable order this cycle")
            journal.sync(gateway, cfg, store.load_state(cfg, "broker"))
            if once:
                return 0
            time.sleep(args.interval)
            continue

        # 5. Risk check the whole batch, then send the passes.
        state = store.load_state(cfg, "broker")
        verdicts = risk_mod.check(orders, cfg, gateway, account=acct, positions=positions,
                                  state=state, now=now)
        store.save_state(cfg, "risk", verdicts)
        store.save_json(cfg, "orders.json", orders)
        print(risk_mod.report(verdicts))
        for r in verdicts["results"]:
            if r["verdict"] != "PASS":
                _decision(cfg, "blocked", symbol=r["symbol"], side=r["side"],
                          reasons=r["reasons"], stage="risk")

        if args.dry_run:
            print("\ndry run: %d order(s) would be sent" % verdicts["pass"])
        else:
            _execute_batch(cfg, gateway, orders, now)
        journal.sync(gateway, cfg, store.load_state(cfg, "broker"))

        if once:
            return 0
        time.sleep(args.interval)


def _execute_batch(cfg, gateway, orders, now):
    sent = 0
    for o in orders:
        ok, why = _ready(cfg, o, now)
        if not ok:
            print("SKIP  %s %s: %s" % (o["side"].upper(), o["symbol"], why))
            continue
        try:
            res, why = _send_order(cfg, gateway, o, now)
        except Exception as e:
            print("ERROR %s %s: %s" % (o["side"].upper(), o["symbol"], e))
            _decision(cfg, "blocked", symbol=o["symbol"], side=o["side"],
                      reasons=["broker: %s" % e], stage="broker")
            continue
        if res is None:
            print("SKIP  %s %s: %s" % (o["side"].upper(), o["symbol"], why))
            continue
        _record_placed(cfg, o, res, now)
        sent += 1
        print("FILLED %s %s %.2f lots @ %s (ticket %s)"
              % (o["side"].upper(), o["symbol"], o["volume"], res.get("price"),
                 res.get("position") or res.get("order")))
        _decision(cfg, "executed", symbol=o["symbol"], side=o["side"], volume=o["volume"],
                  price=res.get("price"), position=res.get("position") or res.get("order"),
                  setup=(o.get("plan") or {}).get("setup"),
                  risk_usd=(o.get("plan") or {}).get("max_loss"), stage="broker")
    print("\n%d order(s) sent" % sent)


# ------------------------------------------------------------------ commands

def cmd_show(args, cfg, gateway, now):
    orders = store.load_json(cfg, "orders.json", []) or []
    if isinstance(orders, dict):
        orders = orders.get("orders") or []
    if not orders:
        print("no orders in trader/orders.json")
        return 0
    rows = []
    for i, o in enumerate(orders, 1):
        ok, why = _ready(cfg, o, now)
        rows.append([i, o.get("symbol"), o.get("side", "").upper(), "%.2f" % o.get("volume", 0),
                     o.get("entry"), o.get("sl"), o.get("tp"),
                     "READY" if ok else "BLOCKED", why[:50]])
    print(cli.table(rows, ["#", "SYMBOL", "SIDE", "LOTS", "ENTRY", "SL", "TP", "STATE", "WHY"]))
    return 0


def cmd_close(args, cfg, gateway, now):
    ps = gateway.positions()
    if args.all:
        targets = ps
    else:
        if not args.symbol:
            raise SystemExit("give a symbol or --all")
        spec = gateway.symbol(args.symbol)
        targets = [p for p in ps if p["symbol"] == spec.name]
    if not targets:
        print("no matching open positions")
        return 0
    if args.volume:
        targets = [dict(p, volume=min(float(args.volume), p["volume"])) for p in targets]
    _close_all(cfg, gateway, now, targets, reason=args.reason, all_positions=True)
    journal.sync(gateway, cfg, store.load_state(cfg, "broker"))
    return 0


def cmd_flatten(args, cfg, gateway, now):
    ps = _positions_ours(gateway, cfg, args.all)
    if not ps:
        print("nothing to flatten")
        return 0
    _close_all(cfg, gateway, now, ps, reason="flatten", all_positions=args.all)
    journal.sync(gateway, cfg, store.load_state(cfg, "broker"))
    return 0


def cmd_sync(args, cfg, gateway):
    res = journal.sync(gateway, cfg, store.load_state(cfg, "broker"), days=args.days)
    print("journal: %d deals, %d closed positions, %d new" % (res["fetched"],
                                                              res["closed_positions"], res["added"]))
    for r in res["records"]:
        print("  %s %s %s %.2f lots  %s pips  %s  %sR"
              % (r["closed"][:16], r["symbol"], r["side"].upper(), r["volume"],
                 r["pips"], r["net"], r["r_multiple"]))
    return 0


def cmd_status(args, cfg, gateway, now):
    acct = _print_account(gateway)
    _print_positions(gateway)
    print("")
    st = store.load_state(cfg, "broker")
    orders = st.get("orders") or []
    today = _today(now)
    print("agent orders today: %d" % len([r for r in orders if r.get("date") == today]))
    rk = _risk_state(cfg)
    if rk:
        print("last risk check: %s  %d PASS, %d BLOCK" % (rk.get("as_of"), rk.get("pass", 0),
                                                           rk.get("block", 0)))
    recs = store.read_jsonl(cfg, "journal.jsonl")
    m = journal.metrics(recs, (cfg.get("account") or {}).get("starting_balance") or 1000)
    print("journal: %d closed trades, net %s, win rate %.1f%%"
          % (m["trades"], m["net"], m["win_rate"]))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Autonomous FX broker (MetaTrader 5)")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("account", help="account summary")
    cli.add_common(a, now=False)

    ps = sub.add_parser("positions", help="open positions")
    cli.add_common(ps, now=False)

    sh = sub.add_parser("show", help="orders and their risk state")
    cli.add_common(sh)

    ex = sub.add_parser("execute", help="send the orders that passed risk")
    cli.add_common(ex)
    ex.add_argument("--allow-live", action="store_true")
    ex.add_argument("--dry-run", action="store_true")

    au = sub.add_parser("auto", help="full autonomous cycle, no prompts")
    cli.add_common(au)
    mode = au.add_mutually_exclusive_group()
    mode.add_argument("--loop", action="store_true", help="keep running every --interval seconds")
    mode.add_argument("--once", action="store_true", help="run a single cycle (default)")
    au.add_argument("--interval", type=int, default=300)
    au.add_argument("--session", default=None)
    au.add_argument("--or-minutes", type=int, default=None)
    au.add_argument("--max-candidates", type=int, default=None)
    au.add_argument("--news", action="store_true", help="refresh the RSS news before scanning")
    au.add_argument("--dry-run", action="store_true")
    au.add_argument("--allow-live", action="store_true")

    cl = sub.add_parser("close", help="close a symbol's position")
    cli.add_common(cl)
    cl.add_argument("symbol", nargs="?", default=None)
    cl.add_argument("--all", action="store_true")
    cl.add_argument("--volume", type=float, default=None)
    cl.add_argument("--reason", default="manual close")

    fl = sub.add_parser("flatten", help="close every position this agent opened")
    cli.add_common(fl)
    fl.add_argument("--all", action="store_true", help="close every position, not just our magic")

    sy = sub.add_parser("sync", help="pull closed deals into trader/journal.jsonl")
    cli.add_common(sy, now=False)
    sy.add_argument("--days", type=int, default=365)

    st = sub.add_parser("status", help="account + positions + journal snapshot")
    cli.add_common(st)

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    now = cli.now_from(args, cfg)
    gateway = cli.require_gateway(cfg)
    try:
        if args.cmd == "account":
            _print_account(gateway)
            return 0
        if args.cmd == "positions":
            _print_positions(gateway)
            return 0
        if args.cmd == "show":
            return cmd_show(args, cfg, gateway, now)
        if args.cmd == "execute":
            return cmd_execute(args, cfg, gateway, now)
        if args.cmd == "auto":
            return cmd_auto(args, cfg, gateway, now, once=not args.loop)
        if args.cmd == "close":
            return cmd_close(args, cfg, gateway, now)
        if args.cmd == "flatten":
            return cmd_flatten(args, cfg, gateway, now)
        if args.cmd == "sync":
            return cmd_sync(args, cfg, gateway)
        return cmd_status(args, cfg, gateway, now)
    finally:
        gateway.shutdown()


if __name__ == "__main__":
    sys.exit(main())
