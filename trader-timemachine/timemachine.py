#!/usr/bin/env python3
"""Time Machine for FX: replay a trade before risking anything.

    python timemachine.py run --plan trader/plan.json --write
    python timemachine.py run --replays 20000 --seed 7 --write

Two questions, the same two the stock pack asked:

1. **Monte Carlo** - draw the rest of the session 10,000 times from this
   pair's own M5 returns at the same time of day over the last 60 sessions.
2. **History replay** - run the exact rule on every past session where it
   fired and count target first, stop first, or still open at the end.

It is a reality check, not a promise. The odds come from the pair's recent
behaviour; news, gaps and central banks can do things those sessions never did.
"""

import argparse
import datetime
import json
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, indicators, store, timeutil
from fx.symbols import format_pips, format_price


class TMError(Exception):
    pass


def _session_bars_for(bars, day, session, interval=5, flat_by=None):
    start, end = timeutil.session_window(day, session)
    if flat_by:
        # A Forex trade is held until flat_by, which can be after the session
        # that produced the entry (London closes at 12:00, flat_by is 16:55).
        end = max(end, timeutil.flat_by_utc(day, flat_by))
    return indicators.session_bars(bars, start, end, end, interval), start, end


def _sessions(bars, now, session, days, flat_by=None):
    """[(day, session_bars, start, end)] for the last ``days`` NY dates."""
    out = []
    today = timeutil.to_ny(now).date()
    for back in range(int(days), -1, -1):
        day = today - datetime.timedelta(days=back)
        sb, start, end = _session_bars_for(bars, day, session, flat_by=flat_by)
        if len(sb) >= 10:
            out.append((day, sb, start, end))
    return out


def _entry_index(sb, entry_time):
    for i, b in enumerate(sb):
        if b["t_utc"] >= entry_time:
            return i
    return None


def build_pool(sessions, entry_idx, max_slots):
    """slot -> [return in pips] from every past session at that slot."""
    pool = {j: [] for j in range(1, max_slots + 1)}
    everything = []
    for day, sb, start, end in sessions:
        if len(sb) <= entry_idx + 1:
            continue
        prev = sb[entry_idx]["c"]
        for j, b in enumerate(sb[entry_idx + 1:], 1):
            r = b["c"] - prev
            prev = b["c"]
            everything.append(r)
            if j in pool:
                pool[j].append(r)
    return pool, everything


def simulate(pool, everything, entry, stop, target, side, pip, last, replays=10000,
             seed=19, window=2):
    """Close-path Monte Carlo. Returns counts and R statistics."""
    rng = random.Random(seed)
    long = side == "buy"
    target_n = stop_n = open_n = 0
    rs = []
    for _ in range(int(replays)):
        px = entry
        outcome = None
        for j in range(1, last + 1):
            choices = []
            for k in range(max(1, j - window), min(last, j + window) + 1):
                choices.extend(pool.get(k) or [])
            if not choices:
                choices = everything
            if not choices:
                break
            move = rng.choice(choices)
            px += move if long else -move
            if long:
                if px <= stop:
                    outcome = "stop"
                    break
                if px >= target:
                    outcome = "target"
                    break
            else:
                if px >= stop:
                    outcome = "stop"
                    break
                if px <= target:
                    outcome = "target"
                    break
        if outcome == "target":
            target_n += 1
        elif outcome == "stop":
            stop_n += 1
        else:
            open_n += 1
        rs.append(1.0 if outcome == "target" else (-1.0 if outcome == "stop" else 0.0))
    n = float(replays)
    return {"replays": int(replays), "target_first": round(target_n / n, 4),
            "stop_first": round(stop_n / n, 4), "open_at_end": round(open_n / n, 4),
            "expectancy_r": round(sum(rs) / n, 4) if n else 0.0}


def history_replay(sessions, entry_idx, risk_price, reward_price, side, last):
    """Run the same rule on every past session. Conservative: stop wins ties."""
    long = side == "buy"
    fired = target_n = stop_n = open_n = 0
    rs = []
    for day, sb, start, end in sessions:
        if len(sb) <= entry_idx + 1:
            continue
        entry = sb[entry_idx]["c"]
        stop = entry - risk_price if long else entry + risk_price
        target = entry + reward_price if long else entry - reward_price
        fired += 1
        outcome = None
        for b in sb[entry_idx + 1:entry_idx + 1 + last]:
            if long:
                if b["l"] <= stop:
                    outcome = "stop"
                    break
                if b["h"] >= target:
                    outcome = "target"
                    break
            else:
                if b["h"] >= stop:
                    outcome = "stop"
                    break
                if b["l"] <= target:
                    outcome = "target"
                    break
        if outcome == "target":
            target_n += 1
            rs.append(1.0)
        elif outcome == "stop":
            stop_n += 1
            rs.append(-1.0)
        else:
            open_n += 1
            rs.append(0.0)
    return {"sessions": len(sessions), "fired": fired, "target": target_n, "stop": stop_n,
            "open": open_n, "win_rate": round(100.0 * target_n / fired, 1) if fired else 0.0,
            "expectancy_r": round(sum(rs) / len(rs), 4) if rs else 0.0}


def verdict(mc):
    e = mc["expectancy_r"]
    if e > 0.10:
        return "positive edge"
    if e < -0.10:
        return "negative edge"
    return "coin flip"


def compute(gateway, plan, cfg, now, replays=10000, seed=19, window=2, days=None):
    spec = gateway.symbol(plan["symbol"])
    side = plan["side"]
    entry = float(plan["entry"])
    stop = float(plan["stop"])
    target = float(plan["target"])
    risk = abs(entry - stop)
    reward = abs(target - entry)
    session = plan.get("session") or ((cfg.get("strategy") or {}).get("orb") or {}).get("session") or "london"
    days = days or int(((cfg.get("strategy") or {}).get("timemachine") or {}).get("days") or 60)
    entry_time = timeutil.parse_utc(plan.get("entry_time")) or now
    flat_by = plan.get("flat_by") or ((cfg.get("risk") or {}).get("flat_by") or "16:55")

    bars = gateway.rates(spec.name, "M5", 60 * 288 + 500)
    sessions = _sessions(bars, now, session, days, flat_by=flat_by)
    if len(sessions) < 5:
        raise TMError("not enough %s sessions of M5 history for %s (%d)" % (session, spec.name, len(sessions)))

    day = timeutil.to_ny(entry_time).date()
    sb, start, end = _session_bars_for(bars, day, session, flat_by=flat_by)
    if len(sb) < 2:
        raise TMError("no %s session bars for %s on %s" % (session, spec.name, day))
    idx = _entry_index(sb, entry_time)
    if idx is None:
        idx = len(sb) - 1
    last = len(sb) - idx - 1
    if last <= 0:
        raise TMError("the entry is at the end of the session: nothing left to replay")

    pool, everything = build_pool(sessions, idx, last)
    mc = simulate(pool, everything, entry, stop, target, side, spec.pip_size, last,
                  replays=replays, seed=seed, window=window)
    hist = history_replay(sessions, idx, risk, reward, side, last)
    v = verdict(mc)
    notes = [
        "target first %.1f%%, stop first %.1f%%, still open %.1f%%"
        % (100 * mc["target_first"], 100 * mc["stop_first"], 100 * mc["open_at_end"]),
        "history: %d sessions, %d fired, %d target, %d stop, %d open"
        % (hist["sessions"], hist["fired"], hist["target"], hist["stop"], hist["open"]),
    ]
    if hist["fired"] and hist["win_rate"] < 40:
        notes.append("the same rule lost more often than it won in the past %d sessions" % hist["sessions"])
    return {
        "as_of": timeutil.iso(now), "symbol": spec.name, "side": side, "setup": plan.get("setup"),
        "session": session, "entry": entry, "stop": stop, "target": target,
        "risk_pips": round(spec.pips(risk), 2), "reward_pips": round(spec.pips(reward), 2),
        "reward_risk": round(reward / risk, 2) if risk else 0.0,
        "entry_time": plan.get("entry_time"), "bars_left": last, "flat_by": flat_by,
        "monte_carlo": mc, "history": hist, "verdict": v, "notes": notes,
        "seed": seed, "window": window, "days": days,
    }


def report(doc):
    spec_line = "%s %s  entry %s  stop %s  target %s  (%s pips risk, %s pips reward)" % (
        doc["side"].upper(), doc["symbol"], doc["entry"], doc["stop"], doc["target"],
        format_pips(doc["risk_pips"]), format_pips(doc["reward_pips"]))
    mc, h = doc["monte_carlo"], doc["history"]
    lines = [spec_line, ""]
    lines.append("Monte Carlo  %d replays, seed %d, %d bars left" % (mc["replays"], doc["seed"], doc["bars_left"]))
    lines.append("  target first   %6.1f%%" % (100 * mc["target_first"]))
    lines.append("  stop first     %6.1f%%" % (100 * mc["stop_first"]))
    lines.append("  open at end    %6.1f%%" % (100 * mc["open_at_end"]))
    lines.append("  expectancy     %+.2f R" % mc["expectancy_r"])
    lines.append("")
    lines.append("History      %d %s sessions" % (h["sessions"], doc["session"]))
    lines.append("  fired          %d" % h["fired"])
    lines.append("  target first   %d" % h["target"])
    lines.append("  stop first     %d" % h["stop"])
    lines.append("  open at end    %d" % h["open"])
    lines.append("  expectancy     %+.2f R" % h["expectancy_r"])
    lines.append("")
    lines.append("Verdict: %s" % doc["verdict"].upper())
    for n in doc["notes"]:
        lines.append("  - " + n)
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description="FX Time Machine")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="simulate the plan in trader/plan.json")
    cli.add_common(r)
    r.add_argument("--plan", default=None)
    r.add_argument("--replays", type=int, default=None)
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--window", type=int, default=None)
    r.add_argument("--days", type=int, default=None)
    r.add_argument("--write", action="store_true")

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    now = cli.now_from(args, cfg)
    tm = (cfg.get("strategy") or {}).get("timemachine") or {}
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
    gateway = cli.require_gateway(cfg)
    try:
        doc = compute(gateway, plan, cfg, now,
                      replays=args.replays or int(tm.get("replays") or 10000),
                      seed=args.seed if args.seed is not None else int(tm.get("seed") or 19),
                      window=args.window if args.window is not None else int(tm.get("window") or 2),
                      days=args.days or int(tm.get("days") or 60))
    except TMError as e:
        raise SystemExit(str(e))
    finally:
        gateway.shutdown()
    if args.write:
        store.save_json(cfg, "timemachine.json", doc)
    if args.json:
        print(json.dumps(doc, indent=2, default=str))
    else:
        print(report(doc))
        if args.write:
            print("\n-> trader/timemachine.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
