"""Pre-trade risk check, translated from trader-risk to FX.

The rules are the original ones, in the units FX uses:

======================  =====================================================
original                here
======================  =====================================================
shares                  lots (volume), snapped to the broker's step
price x shares          margin (leverage 1:1000)
max_loss_per_trade_usd  lots x money risk per lot at the stop
max_position_pct        max_margin_pct of equity committed as margin
cash buffer             free margin that must stay untouched
short selling off       allow_short (true by default here)
======================  =====================================================

Exits are never blocked by entry rules: getting out must always work.
"""

import datetime
import hashlib
import json

from . import timeutil
from .symbols import money, format_pips, format_price

FRESH_MINUTES = 15
TYPES = ("market", "limit")
CENT = 1e-9


class RiskError(Exception):
    pass


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fingerprint(order):
    keep = ("symbol", "side", "volume", "type", "entry", "sl", "tp", "comment")
    raw = {k: order.get(k) for k in keep}
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def today_ny(now_utc):
    return timeutil.to_ny(now_utc).date().isoformat()


def placed_today(state, today):
    return [r for r in (state or {}).get("orders", [])
            if r.get("date") == today and r.get("status") in ("placed", "filled", "partial")]


def rec_is_entry(rec):
    return str(rec.get("kind") or "entry").lower() == "entry"


def _hhmm(text):
    if text in (None, "", False):
        return None
    try:
        h, m = [int(x) for x in str(text).split(":")]
        return datetime.time(h, m)
    except (ValueError, TypeError):
        raise RiskError("a time must look like HH:MM (got %r)" % text)


def window_check(now_utc, cfg):
    ny = timeutil.to_ny(now_utc)
    t = ny.time()
    before = _hhmm(cfg.get("no_entries_before"))
    after = _hhmm(cfg.get("no_entries_after"))
    reasons = []
    if before and t < before:
        reasons.append("window: it is %s New York, before your %s first entry"
                       % (t.strftime("%H:%M"), before.strftime("%H:%M")))
    if after and t > after:
        reasons.append("window: it is %s New York, after your %s last entry"
                       % (t.strftime("%H:%M"), after.strftime("%H:%M")))
    return reasons


def _held(positions, symbol):
    long_v = sum(float(p.get("volume") or 0) for p in positions
                 if p.get("symbol") == symbol and p.get("side") == "buy")
    short_v = sum(float(p.get("volume") or 0) for p in positions
                  if p.get("symbol") == symbol and p.get("side") == "sell")
    return long_v, short_v


def check(orders, cfg, gateway, account=None, positions=None, state=None, now=None, budget=None):
    """Pure-ish: reads the gateway for ticks/margins, returns the verdicts."""
    now = now or timeutil.utc_now()
    rcfg = cfg.get("risk") or {}
    acfg = cfg.get("account") or {}
    account = account or gateway.account()
    positions = positions if positions is not None else gateway.positions()
    state = state or {}
    today = today_ny(now)
    equity = float(account.get("equity") or account.get("balance") or 0)
    free = float(account.get("margin_free") or 0)
    used_margin = float(account.get("margin") or 0)
    buffer_pct = float(rcfg.get("cash_buffer_pct") or 0)
    buffer_usd = equity * buffer_pct / 100.0
    is_open, closed_reason = timeutil.market_open(now)

    try:
        term = gateway.terminal()
    except Exception:
        term = {}
    algo_ok = bool(term.get("trade_allowed", True))
    algo_disabled = bool(term.get("tradeapi_disabled", False))

    blocked = {str(b).upper() for b in (rcfg.get("blocked") or [])}
    done_today = placed_today(state, today)
    entries_today = [r for r in done_today if rec_is_entry(r)]
    loss_today = sum(float(r.get("max_loss") or 0) for r in entries_today)
    placed_keys = {(str(r.get("symbol", "")).upper(), str(r.get("side", "")).lower()) for r in done_today}
    win_reasons = window_check(now, rcfg)

    results = []
    seen = set()
    batch_entries = 0
    batch_loss = 0.0

    for i, o in enumerate(orders, 1):
        reasons, warns, info = [], [], []
        later = []  # entry-only rules; become warnings on an exit
        sym = str(o.get("symbol") or "").strip().upper()
        side = str(o.get("side") or "").strip().lower()
        typ = str(o.get("type") or "market").strip().lower()
        vol = _f(o.get("volume"))
        plan = o.get("plan") if isinstance(o.get("plan"), dict) else {}
        spec = None
        tick = None
        try:
            spec = gateway.symbol(sym) if sym else None
        except Exception as e:
            reasons.append("asset: %s" % e)

        # ---------------------------------------------------------- format
        if not sym:
            reasons.append("format: no symbol")
        if side not in ("buy", "sell"):
            reasons.append("format: side must be buy or sell, got %r" % o.get("side"))
        if vol is None or vol <= 0:
            reasons.append("format: volume must be a positive number of lots, got %r" % o.get("volume"))
        if typ not in TYPES:
            reasons.append("format: unknown order type %r (market or limit)" % typ)
        if typ == "limit" and _f(o.get("price") or o.get("entry")) is None:
            reasons.append("format: a limit order needs a price")
        if spec and vol:
            step = spec.volume_step or 0.01
            if abs(vol / step - round(vol / step)) > 1e-6:
                reasons.append("format: %s is not a multiple of the %s lot step"
                               % (format_pips(vol), format_pips(step)))
            if vol < spec.volume_min - 1e-9:
                reasons.append("format: %s is under the broker's %s minimum lot"
                               % (format_pips(vol), format_pips(spec.volume_min)))
            if vol > spec.volume_max + 1e-9:
                reasons.append("format: %s is over the broker's %s maximum lot"
                               % (format_pips(vol), format_pips(spec.volume_max)))

        # ---------------------------------------------------------- asset
        if spec:
            if not spec.is_forex:
                reasons.append("asset: %s is not an FX pair; this pack trades currency pairs only" % sym)
            if spec.trade_mode != 4:
                reasons.append("asset: %s is not fully tradable (trade_mode %s)" % (sym, spec.trade_mode))
            if sym in blocked:
                later.append("blocked: %s is on your blocked list" % sym)
            try:
                tick = gateway.tick(spec.name)
            except Exception as e:
                later.append("price: no tick for %s (%s)" % (sym, e))

        # ---------------------------------------------------------- entry or exit
        long_held, short_held = _held(positions, spec.name) if spec else (0.0, 0.0)
        exiting = False
        if side == "sell" and long_held > 1e-9 and vol is not None:
            exiting = vol <= long_held + 1e-9
        elif side == "buy" and short_held > 1e-9 and vol is not None:
            exiting = vol <= short_held + 1e-9
        if o.get("close_ticket"):
            exiting = True
        kind = "exit" if exiting else "entry"

        # ---------------------------------------------------------- reference price
        plan_entry = _f(plan.get("entry"))
        stop = _f(o.get("sl")) or _f(plan.get("stop"))
        target = _f(o.get("tp")) or _f(plan.get("target"))
        ref = None
        ref_from = None
        if typ == "limit":
            ref = _f(o.get("price") or o.get("entry")) or plan_entry
            ref_from = "limit"
        elif tick and plan_entry:
            if side == "buy":
                ref = max(plan_entry, tick["ask"])
            else:
                ref = min(plan_entry, tick["bid"])
            ref_from = "price" if ref != plan_entry else "plan"
        elif tick:
            ref = tick["ask"] if side == "buy" else tick["bid"]
            ref_from = "price"
        elif plan_entry:
            ref = plan_entry
            ref_from = "plan"

        if ref and spec:
            ref = spec.round_price(ref)
        risk_pips = risk_usd = margin_usd = rr = None
        if spec and ref and stop:
            risk_pips = spec.pips(abs(ref - stop))
        if spec and ref and stop and vol:
            try:
                risk_per_lot = gateway.risk_per_lot(side, spec.name, ref, stop)
                risk_usd = round(vol * risk_per_lot, 2)
            except Exception as e:
                later.append("risk: could not price the stop (%s)" % e)
        if spec and ref and vol:
            try:
                margin_usd = round(gateway.calc_margin(side, spec.name, vol, ref) or 0.0, 2)
            except Exception:
                margin_usd = None
        if ref and stop and target and abs(ref - stop) > CENT:
            rr = round(abs(target - ref) / abs(ref - stop), 2)

        # ---------------------------------------------------------- size / margin
        if vol is not None:
            min_lots = _f(rcfg.get("min_order_lots"))
            max_lots = _f(rcfg.get("max_order_lots"))
            if min_lots and vol < min_lots - 1e-9:
                later.append("size: %s lots is under your %s minimum order"
                             % (format_pips(vol), format_pips(min_lots)))
            if max_lots and vol > max_lots + 1e-9:
                later.append("size: %s lots is over your %s maximum order"
                             % (format_pips(vol), format_pips(max_lots)))
        if margin_usd is not None:
            if margin_usd > free + CENT:
                later.append("margin: needs %s but only %s free margin is available"
                             % (money(margin_usd), money(free)))
            elif free - margin_usd < buffer_usd - CENT:
                later.append("margin: leaves %s free, under your %s%% buffer of %s"
                             % (money(max(free - margin_usd, 0)), format_pips(buffer_pct),
                                money(buffer_usd)))
            cap_pct = _f(rcfg.get("max_margin_pct"))
            if cap_pct and equity > 0 and used_margin + margin_usd > equity * cap_pct / 100.0 + CENT:
                later.append("margin: %s used + %s new = %.1f%% of the %s account, over your %s%% cap"
                             % (money(used_margin), money(margin_usd),
                                100.0 * (used_margin + margin_usd) / equity, money(equity),
                                format_pips(cap_pct)))
            if not rcfg.get("allow_margin", True) and margin_usd > equity + CENT:
                later.append("margin: this would borrow money and allow_margin is off")

        # ---------------------------------------------------------- entry rules
        if kind == "entry":
            if not algo_ok:
                later.append("terminal: algo trading is OFF. Enable Tools > Options > Expert Advisors "
                             "> Allow Algo Trading in MetaTrader 5")
            if algo_disabled:
                later.append("terminal: the broker disabled the trading API for this terminal")
            if not is_open:
                later.append("market: %s" % (closed_reason or "closed"))
            if tick and tick.get("time_utc"):
                age = (now - tick["time_utc"]).total_seconds()
                if age > FRESH_MINUTES * 60:
                    later.append("price: the last %s tick is %d minutes old"
                                 % (sym, int(age // 60)))
            later.extend(win_reasons)
            max_spread = _f(rcfg.get("max_spread_pips"))
            if max_spread is not None and tick and tick.get("spread_pips", 0) > max_spread + 1e-9:
                later.append("spread: %.1f pips is over your %.1f pip limit"
                             % (tick["spread_pips"], max_spread))

            if not stop:
                if rcfg.get("require_stop", True):
                    later.append("stop: no stop. Every entry needs one (require_stop true): a bracket "
                                 "SL from trader-exit and trader-budget")
                else:
                    warns.append("no stop: the max loss is not capped")
            elif ref and spec:
                if (side == "buy" and stop >= ref - CENT) or (side == "sell" and stop <= ref + CENT):
                    later.append("stop: the stop %s must be %s the entry %s for a %s"
                                 % (format_price(stop, spec), "below" if side == "buy" else "above",
                                    format_price(ref, spec), side))
                if target:
                    if (side == "buy" and target <= ref + CENT) or (side == "sell" and target >= ref - CENT):
                        later.append("target: the target %s must be %s the entry %s"
                                     % (format_price(target, spec),
                                        "above" if side == "buy" else "below",
                                        format_price(ref, spec)))
            cap = _f(rcfg.get("max_loss_per_trade_usd"))
            if cap and risk_usd is not None and risk_usd > cap + 0.005:
                later.append("loss: %s lots x %s risk per lot = %s at the stop, over your %s max loss "
                             "per trade" % (format_pips(vol), money(risk_usd / vol if vol else 0),
                                            money(risk_usd), money(cap)))
            min_rr = _f(rcfg.get("min_reward_risk"))
            if min_rr and target and rr is not None and rr < min_rr - 1e-9:
                later.append("reward:risk: %.2f (target %s, entry %s, stop %s) is under your minimum %s"
                             % (rr, format_price(target, spec), format_price(ref, spec),
                                format_price(stop, spec), format_pips(min_rr)))
            elif min_rr and not target:
                later.append("reward:risk: no target to measure it against (min_reward_risk %s)"
                             % format_pips(min_rr))
            daily = _f(rcfg.get("max_daily_loss_usd"))
            if daily and risk_usd is not None:
                used = loss_today + batch_loss
                if used + risk_usd > daily + 0.005:
                    later.append("daily loss: %s already at risk today plus %s for this one = %s, over "
                                 "your %s daily limit" % (money(used), money(risk_usd),
                                                          money(used + risk_usd), money(daily)))
            cap_t = int(rcfg.get("max_trades_per_day") or 0)
            if cap_t and len(entries_today) + batch_entries >= cap_t:
                later.append("trades: %d entries placed today plus %d passed earlier in this batch, your "
                             "limit is %d a day" % (len(entries_today), batch_entries, cap_t))
            if side == "sell" and not rcfg.get("allow_short", True):
                later.append("short: short selling is off (allow_short false)")
            if o.get("_short_from_flat") and not rcfg.get("allow_short", True):
                later.append("short: short selling is off (allow_short false)")

            key = (sym, side)
            if key in seen:
                later.append("duplicate: a second %s %s in this batch" % (side, sym))
            if key in placed_keys:
                later.append("duplicate: a %s %s was already placed today" % (side, sym))
            cap_n = int(rcfg.get("max_orders_per_day") or 0)
            passed_so_far = sum(1 for r in results if r["verdict"] == "PASS")
            if cap_n and len(done_today) + passed_so_far >= cap_n:
                later.append("daily limit: %d placed today plus %d passed earlier in this batch, your "
                             "limit is %d a day" % (len(done_today), passed_so_far, cap_n))
            seen.add(key)

            if stop and ref and risk_usd is not None:
                info.append("%s entry %s, stop %s (%.1f pips), target %s: risk %s"
                            % (side.upper(), format_price(ref, spec), format_price(stop, spec),
                               risk_pips or 0, format_price(target, spec) if target else "none",
                               money(risk_usd)))
                info.append("margin %s, reward:risk %s, today %d/%s trades, %s/%s daily loss"
                            % (money(margin_usd) if margin_usd is not None else "n/a",
                               ("%.2f" % rr) if rr is not None else "n/a",
                               len(entries_today) + batch_entries,
                               cap_t or "unlimited", money(loss_today + batch_loss),
                               money(daily) if daily else "no limit"))
        else:
            warns.append("exit: entry rules are not applied to a position-reducing order")

        reasons.extend(later if kind == "entry" else [])
        if kind == "exit":
            warns.extend("exit, not blocked: " + x for x in later)

        verdict = "BLOCK" if reasons else "PASS"
        if verdict == "PASS":
            if kind == "entry":
                batch_entries += 1
                batch_loss += risk_usd or 0.0
            if margin_usd:
                used_margin += margin_usd
                free -= margin_usd

        results.append({
            "index": i, "symbol": sym, "side": side, "kind": kind,
            "volume": vol, "amount": ("%s lots" % format_pips(vol)) if vol else None,
            "entry_ref": ref, "entry_ref_from": ref_from,
            "stop": stop, "target": target, "risk_pips": round(risk_pips, 2) if risk_pips else None,
            "risk_usd": risk_usd, "margin_usd": margin_usd, "reward_risk": rr,
            "verdict": verdict, "reasons": reasons, "warnings": warns, "info": info,
            "fingerprint": fingerprint(o),
        })

    return {
        "as_of": timeutil.iso(now), "today": today,
        "pass": sum(1 for r in results if r["verdict"] == "PASS"),
        "block": sum(1 for r in results if r["verdict"] == "BLOCK"),
        "results": results,
        "warnings": ([] if is_open else ["the FX market is closed: %s" % closed_reason])
                    + ([] if algo_ok else ["algo trading is OFF in the terminal"]),
    }


def report(res):
    lines = []
    for r in res["results"]:
        head = "%d. %s %s %s" % (r["index"], r["verdict"], str(r["side"]).upper(), r["symbol"])
        if r.get("volume"):
            head += " %s lots" % format_pips(r["volume"])
        lines.append(head)
        for x in r.get("reasons") or []:
            lines.append("   BLOCK  " + x)
        for x in r.get("warnings") or []:
            lines.append("   warn   " + x)
        for x in r.get("info") or []:
            lines.append("   " + x)
    lines.append("")
    lines.append("%d PASS, %d BLOCK" % (res["pass"], res["block"]))
    return "\n".join(lines)
