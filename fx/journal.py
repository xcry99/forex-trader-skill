"""Trade journal and performance metrics.

MetaTrader keeps the truth: every closed position is a pair of deals. This
module folds those deals into one record per position, attaches the agent's
plan (setup, risk, confidence, timemachine odds) when it can match the
position ticket, and computes the numbers you would ask a trader for: win
rate, profit factor, expectancy in R, drawdown, and the breakdown by setup
and session.
"""

import datetime
import math

from . import timeutil, store
from .symbols import format_pips


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _position_records(deals, gateway, state):
    """Group raw deals by position_id -> closed trade records."""
    groups = {}
    for d in deals:
        pid = d.get("position_id")
        if pid is None:
            continue
        groups.setdefault(pid, []).append(d)
    plans = {}
    for r in (state or {}).get("orders", []):
        for key in ("position", "position_id", "ticket"):
            if r.get(key):
                plans[int(r[key])] = r
    out = []
    for pid, rows in groups.items():
        rows.sort(key=lambda x: x.get("time_utc") or datetime.datetime.min)
        entries = [d for d in rows if int(d.get("entry", 0)) == 0]
        exits = [d for d in rows if int(d.get("entry", 0)) == 1]
        if not entries or not exits:
            continue
        vol = sum(float(d.get("volume") or 0) for d in entries) or 1.0
        entry_px = sum(float(d.get("price") or 0) * float(d.get("volume") or 0) for d in entries) / vol
        exit_vol = sum(float(d.get("volume") or 0) for d in exits) or 1.0
        exit_px = sum(float(d.get("price") or 0) * float(d.get("volume") or 0) for d in exits) / exit_vol
        direction = "buy" if int(entries[0].get("type", 0)) == 0 else "sell"
        symbol = entries[0].get("symbol")
        gross = sum(float(d.get("profit") or 0) for d in rows)
        commission = sum(float(d.get("commission") or 0) for d in rows)
        swap = sum(float(d.get("swap") or 0) for d in rows)
        net = round(gross + commission + swap, 2)
        try:
            spec = gateway.symbol(symbol)
            pips = spec.pips((exit_px - entry_px) * (1 if direction == "buy" else -1))
        except Exception:
            pips = None
        plan = plans.get(int(pid)) or plans.get(int(entries[0].get("ticket") or 0)) or {}
        risk = _f(plan.get("max_loss")) or _f((plan.get("plan") or {}).get("max_loss"))
        out.append({
            "id": "pos-%s" % pid,
            "position_id": pid,
            "ticket": entries[0].get("ticket"),
            "symbol": symbol,
            "side": direction,
            "volume": round(vol, 4),
            "entry_price": round(entry_px, 6),
            "exit_price": round(exit_px, 6),
            "pips": round(pips, 2) if pips is not None else None,
            "gross": round(gross, 2),
            "commission": round(commission, 2),
            "swap": round(swap, 2),
            "net": net,
            "risk_usd": risk,
            "r_multiple": round(net / risk, 3) if risk else None,
            "setup": plan.get("setup") or (plan.get("plan") or {}).get("setup"),
            "session": plan.get("session") or (plan.get("plan") or {}).get("session"),
            "confidence": (plan.get("plan") or {}).get("confidence"),
            "timemachine": (plan.get("plan") or {}).get("timemachine"),
            "magic": entries[0].get("magic"),
            "comment": entries[0].get("comment"),
            "opened": timeutil.iso(min(d["time_utc"] for d in entries)),
            "closed": timeutil.iso(max(d["time_utc"] for d in exits)),
        })
    out.sort(key=lambda r: r.get("closed") or "")
    return out


def sync(gateway, cfg, state=None, days=365):
    """Pull deals from MT5 and append the positions not yet in the journal."""
    now = timeutil.utc_now()
    start = now - datetime.timedelta(days=int(days))
    deals = gateway.deals(start, now + datetime.timedelta(minutes=5))
    recs = _position_records(deals, gateway, state or {})
    existing = {r.get("id") for r in store.read_jsonl(cfg, "journal.jsonl")}
    added = [r for r in recs if r["id"] not in existing]
    for r in added:
        store.append_jsonl(cfg, "journal.jsonl", r)
    return {"fetched": len(deals), "closed_positions": len(recs), "added": len(added),
            "records": added, "as_of": timeutil.iso(now)}


# ------------------------------------------------------------------ metrics


def _stats(rows):
    n = len(rows)
    if not n:
        return {"trades": 0, "wins": 0, "losses": 0, "breakeven": 0, "win_rate": 0.0,
                "gross_profit": 0.0, "gross_loss": 0.0, "net": 0.0, "profit_factor": 0.0,
                "expectancy": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "payoff": 0.0,
                "avg_r": None, "total_pips": 0.0, "avg_pips": None}
    nets = [float(r.get("net") or 0) for r in rows]
    wins = [x for x in nets if x > 1e-9]
    losses = [x for x in nets if x < -1e-9]
    rs = [float(r["r_multiple"]) for r in rows if r.get("r_multiple") is not None]
    pips = [float(r["pips"]) for r in rows if r.get("pips") is not None]
    gp = sum(wins)
    gl = abs(sum(losses))
    return {
        "trades": n, "wins": len(wins), "losses": len(losses), "breakeven": n - len(wins) - len(losses),
        "win_rate": round(100.0 * len(wins) / n, 2),
        "gross_profit": round(gp, 2), "gross_loss": round(gl, 2),
        "net": round(sum(nets), 2),
        "profit_factor": round(gp / gl, 2) if gl else (999.0 if gp else 0.0),
        "expectancy": round(sum(nets) / n, 2),
        "avg_win": round(gp / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gl / len(losses), 2) if losses else 0.0,
        "payoff": round((gp / len(wins)) / (gl / len(losses)), 2) if wins and losses else 0.0,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "total_pips": round(sum(pips), 1) if pips else 0.0,
        "avg_pips": round(sum(pips) / len(pips), 2) if pips else None,
    }


def _max_drawdown(nets, starting):
    equity = float(starting or 0)
    peak = equity
    worst = 0.0
    worst_pct = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        dd = peak - equity
        if dd > worst:
            worst = dd
            worst_pct = 100.0 * dd / peak if peak else 0.0
    return round(worst, 2), round(worst_pct, 2)


def _group(rows, key):
    out = {}
    for r in rows:
        k = r.get(key) or "unknown"
        out.setdefault(k, []).append(r)
    return {k: _stats(v) for k, v in sorted(out.items(), key=lambda kv: -len(kv[1]))}


def metrics(records, starting_balance=1000.0):
    rows = [r for r in records if r.get("net") is not None]
    rows.sort(key=lambda r: r.get("closed") or "")
    s = _stats(rows)
    nets = [float(r.get("net") or 0) for r in rows]
    dd, dd_pct = _max_drawdown(nets, starting_balance)
    mean = sum(nets) / len(nets) if nets else 0.0
    var = sum((x - mean) ** 2 for x in nets) / len(nets) if len(nets) > 1 else 0.0
    sharpe = (mean / math.sqrt(var) * math.sqrt(len(nets))) if var > 0 else None
    # longest winning / losing streak
    streak = best_win = best_loss = 0
    for x in nets:
        if x > 1e-9:
            streak = streak + 1 if streak > 0 else 1
            best_win = max(best_win, streak)
        elif x < -1e-9:
            streak = streak - 1 if streak < 0 else -1
            best_loss = min(best_loss, streak)
        else:
            streak = 0
    equity = float(starting_balance or 0)
    curve = []
    for r in rows:
        equity += float(r.get("net") or 0)
        curve.append({"closed": r.get("closed"), "net": r.get("net"), "equity": round(equity, 2)})
    s.update({
        "starting_balance": starting_balance,
        "ending_balance": round(equity, 2),
        "return_pct": round(100.0 * (equity / starting_balance - 1.0), 2) if starting_balance else None,
        "max_drawdown": dd, "max_drawdown_pct": dd_pct,
        "sharpe_per_trade": round(sharpe, 2) if sharpe is not None else None,
        "best_win_streak": best_win, "worst_loss_streak": best_loss,
        "by_symbol": _group(rows, "symbol"),
        "by_setup": _group(rows, "setup"),
        "by_session": _group(rows, "session"),
        "best_trade": max(rows, key=lambda r: float(r.get("net") or 0), default=None),
        "worst_trade": min(rows, key=lambda r: float(r.get("net") or 0), default=None),
        "equity_curve": curve,
    })
    return s


def decision_stats(decisions):
    rows = decisions or []
    out = {"cycles": len([r for r in rows if r.get("type") == "cycle"]),
           "candidates": len([r for r in rows if r.get("type") == "candidate"]),
           "executed": len([r for r in rows if r.get("type") == "executed"]),
           "blocked": len([r for r in rows if r.get("type") == "blocked"]),
           "skipped": len([r for r in rows if r.get("type") == "skipped"]),
           "closed": len([r for r in rows if r.get("type") == "closed"]),
           "reasons": {}}
    for r in rows:
        if r.get("type") != "blocked":
            continue
        for reason in r.get("reasons") or []:
            key = str(reason).split(":", 1)[0]
            out["reasons"][key] = out["reasons"].get(key, 0) + 1
    return out


def report(metrics_doc, decisions=None, open_positions=None):
    m = metrics_doc
    lines = []
    lines.append("FOREX TRADER - PERFORMANCE")
    lines.append("=" * 60)
    lines.append("Starting balance : %s" % m.get("starting_balance"))
    lines.append("Ending balance   : %s" % m.get("ending_balance"))
    lines.append("Return           : %s%%" % m.get("return_pct"))
    lines.append("Trades           : %d (%d W / %d L / %d BE)"
                 % (m["trades"], m["wins"], m["losses"], m["breakeven"]))
    lines.append("Win rate         : %.2f%%" % m["win_rate"])
    lines.append("Net              : %s" % m["net"])
    lines.append("Profit factor    : %s" % m["profit_factor"])
    lines.append("Expectancy       : %s per trade" % m["expectancy"])
    lines.append("Avg win / loss   : %s / %s" % (m["avg_win"], m["avg_loss"]))
    lines.append("Avg R            : %s" % (m["avg_r"] if m["avg_r"] is not None else "n/a"))
    lines.append("Total pips       : %s" % m["total_pips"])
    lines.append("Max drawdown     : %s (%.2f%%)" % (m["max_drawdown"], m["max_drawdown_pct"]))
    lines.append("Sharpe/trade     : %s" % (m["sharpe_per_trade"] if m["sharpe_per_trade"] is not None else "n/a"))
    lines.append("Streaks          : %d wins / %d losses"
                 % (m["best_win_streak"], abs(m["worst_loss_streak"])))
    if open_positions is not None:
        lines.append("Open positions   : %d" % open_positions)
    if m.get("by_symbol"):
        lines.append("")
        lines.append("By symbol:")
        for k, v in m["by_symbol"].items():
            lines.append("  %-8s %3d trades  %8s  win %5.1f%%  pips %s"
                         % (k, v["trades"], v["net"], v["win_rate"], v["total_pips"]))
    if m.get("by_setup"):
        lines.append("")
        lines.append("By setup:")
        for k, v in m["by_setup"].items():
            lines.append("  %-18s %3d trades  %8s  win %5.1f%%  avg R %s"
                         % (k, v["trades"], v["net"], v["win_rate"],
                            v["avg_r"] if v["avg_r"] is not None else "n/a"))
    if m.get("by_session"):
        lines.append("")
        lines.append("By session:")
        for k, v in m["by_session"].items():
            lines.append("  %-10s %3d trades  %8s  win %5.1f%%"
                         % (k, v["trades"], v["net"], v["win_rate"]))
    if decisions:
        d = decision_stats(decisions)
        lines.append("")
        lines.append("Agent decisions: %d cycles, %d candidates, %d executed, %d blocked, %d skipped"
                     % (d["cycles"], d["candidates"], d["executed"], d["blocked"], d["skipped"]))
        if d["reasons"]:
            lines.append("  blocks by rule: " + ", ".join("%s=%d" % kv for kv in
                                                          sorted(d["reasons"].items(), key=lambda x: -x[1])))
    return "\n".join(lines)


def to_csv(records):
    cols = ["closed", "symbol", "side", "volume", "entry_price", "exit_price", "pips", "net",
            "risk_usd", "r_multiple", "setup", "session", "confidence", "opened", "comment"]
    out = [",".join(cols)]
    for r in records:
        out.append(",".join(_csv(r.get(c)) for c in cols))
    return "\n".join(out)


def _csv(v):
    if v is None:
        return ""
    s = str(v)
    if any(ch in s for ch in ',"\n'):
        s = '"' + s.replace('"', '""') + '"'
    return s
