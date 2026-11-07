"""Position sizing in lots, pips and margin.

The original ``trader-budget`` divided dollars by (entry - stop) to get whole
shares. FX has a contract size, a pip and a leverage, so the same question
becomes: how many lots put exactly the max-loss budget at risk at the stop?

Everything is measured with the broker's own ``order_calc_profit`` /
``order_calc_margin`` when a gateway is available, so JPY crosses, cross
pairs and non-USD accounts come out right instead of approximated.
"""

from .symbols import money, format_pips


class SizeError(Exception):
    pass


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def reference_price(plan, spec, tick, order_type="market"):
    """The price the risk is measured from.

    A market buy can fill at the ask, so the conservative reference is the
    *higher* of the plan and the ask. A market sell uses the lower of the plan
    and the bid. A limit order uses its own price.
    """
    side = str(plan.get("side") or "").lower()
    entry = _f(plan.get("entry"))
    if entry is None or entry <= 0:
        raise SizeError("the plan needs a positive entry")
    if order_type != "market":
        return spec.round_price(entry), "limit"
    if side == "buy":
        px = max(entry, _f(tick.get("ask")) or entry)
        return spec.round_price(px), "price" if px > entry else "plan"
    px = min(entry, _f(tick.get("bid")) or entry)
    return spec.round_price(px), "price" if px < entry else "plan"


def size_plan(plan, cfg, spec, gateway, account=None, budget=None, tick=None, order_type="market"):
    """One plan + the risk config -> the lot size, margin and max loss."""
    rcfg = cfg.get("risk") or {}
    acfg = cfg.get("account") or {}
    if not isinstance(plan, dict):
        raise SizeError("the plan is not a JSON object")
    side = str(plan.get("side") or "").lower()
    if side not in ("buy", "sell"):
        raise SizeError("the plan's side must be buy or sell (got %r)" % plan.get("side"))
    sym = str(plan.get("symbol") or spec.name).upper()
    entry_plan, stop, target = _f(plan.get("entry")), _f(plan.get("stop")), _f(plan.get("target"))
    for name, v in (("entry", entry_plan), ("stop", stop), ("target", target)):
        if v is None or v <= 0:
            raise SizeError("the plan needs a positive %s (run trader-exit)" % name)

    budget = float(budget if budget is not None else (acfg.get("budget") or 0))
    if budget <= 0:
        raise SizeError("no budget: set account.budget in trader/config.json")
    buf_pct = float(rcfg.get("cash_buffer_pct") or 0)
    if not 0 <= buf_pct < 100:
        raise SizeError("cash_buffer_pct must be between 0 and 100")

    tick = tick or gateway.tick(spec.name)
    ref, ref_from = reference_price(plan, spec, tick, order_type)
    stop, target = spec.round_price(stop), spec.round_price(target)
    long = side == "buy"
    if long and stop >= ref:
        raise SizeError("the stop %s is not below the entry %s: a long needs its stop underneath"
                        % (stop, ref))
    if not long and stop <= ref:
        raise SizeError("the stop %s is not above the entry %s: a short needs its stop overhead"
                        % (stop, ref))
    if long and target <= ref:
        raise SizeError("the target %s is not above the entry %s: the move already happened"
                        % (target, ref))
    if not long and target >= ref:
        raise SizeError("the target %s is not below the entry %s: the move already happened"
                        % (target, ref))

    risk_price = abs(ref - stop)
    risk_pips = spec.pips(risk_price)
    reward_pips = spec.pips(abs(target - ref))
    risk_per_lot = gateway.risk_per_lot(side, spec.name, ref, stop)
    if not risk_per_lot or risk_per_lot <= 0:
        raise SizeError("could not price the risk on %s" % spec.name)
    margin_per_lot = gateway.calc_margin(side, spec.name, 1.0, ref) or 0.0

    acct = account or gateway.account()
    equity = float(acct.get("equity") or acct.get("balance") or 0)
    free = float(acct.get("margin_free") or 0)
    cash_buffer = round(budget * buf_pct / 100.0, 2)
    budget_available = max(budget - cash_buffer, 0.0)
    account_available = max(free - cash_buffer, 0.0)
    available = min(budget_available, account_available) if free else budget_available

    limits = []
    max_loss = _f(rcfg.get("max_loss_per_trade_usd"))
    if max_loss:
        limits.append({"name": "max loss", "lots": max_loss / risk_per_lot,
                       "how": "%s max loss / %s risk per lot"
                              % (money(max_loss), money(risk_per_lot))})
    cap_pct = _f(rcfg.get("max_margin_pct"))
    if cap_pct and margin_per_lot:
        cap = budget * cap_pct / 100.0
        limits.append({"name": "margin cap", "lots": cap / margin_per_lot,
                       "how": "%s%% of %s = %s margin / %s per lot"
                              % (format_pips(cap_pct), money(budget), money(cap), money(margin_per_lot))})
    max_order = _f(rcfg.get("max_order_lots"))
    if max_order:
        limits.append({"name": "max order", "lots": max_order,
                       "how": "%s lots max" % format_pips(max_order)})
    if margin_per_lot:
        limits.append({"name": "margin free", "lots": available / margin_per_lot,
                       "how": "%s available / %s per lot" % (money(available), money(margin_per_lot))})

    raw = min(l["lots"] for l in limits) if limits else 0.0
    volume = spec.round_volume(raw, "down")
    if volume + 1e-9 < spec.volume_min:
        volume = 0.0
    bound = [l["name"] for l in limits if abs(l["lots"] - raw) < 1e-9]
    for l in limits:
        l["binds"] = l["name"] == bound[0] if bound else False
        l["lots"] = round(l["lots"], 4)

    cost = round(volume * margin_per_lot, 2)
    loss = round(volume * risk_per_lot, 2)
    pip_value = round(risk_per_lot / risk_pips, 2) if risk_pips else 0.0
    refusal = None
    if volume <= 0:
        first = bound[0] if bound else "max loss"
        if first == "max loss":
            min_loss = round(spec.volume_min * risk_per_lot, 2)
            refusal = ("one minimum lot (%s) risks %s from %s to %s (%.1f pips), more than your %s "
                       "max loss per trade. No order. Skip it, or plan a closer stop."
                       % (spec.volume_min, money(min_loss), ref, stop, risk_pips, money(max_loss or 0)))
        else:
            how = next((l["how"] for l in limits if l["name"] == first), first)
            refusal = "the smallest lot does not fit: %s. No order." % how
    elif _f(rcfg.get("min_reward_risk")) and reward_pips < _f(rcfg["min_reward_risk"]) * risk_pips - 1e-9:
        refusal = ("reward:risk %.2f (%.1f pips target / %.1f pips risk) is under your minimum %s. "
                   "No order." % (reward_pips / risk_pips if risk_pips else 0, reward_pips, risk_pips,
                                  format_pips(rcfg["min_reward_risk"])))
    elif _f(rcfg.get("min_order_lots")) and volume < _f(rcfg["min_order_lots"]) - 1e-9:
        refusal = ("%s lots is under your %s minimum order. No order."
                   % (format_pips(volume), format_pips(rcfg["min_order_lots"])))

    return {
        "symbol": spec.name, "side": side, "setup": plan.get("setup", ""),
        "session": plan.get("session", ""), "date": plan.get("date"),
        "entry_time": plan.get("entry_time", ""),
        "entry_plan": spec.round_price(entry_plan), "entry": ref, "entry_from": ref_from,
        "stop": stop, "target": target,
        "risk_pips": round(risk_pips, 2), "reward_pips": round(reward_pips, 2),
        "risk_per_lot": round(risk_per_lot, 2), "pip_value_per_lot": pip_value,
        "reward_risk": round(reward_pips / risk_pips, 2) if risk_pips else 0.0,
        "stop_label": plan.get("stop_label", ""), "target_label": plan.get("target_label", ""),
        "budget": budget, "cash_buffer_pct": buf_pct, "cash_buffer": cash_buffer,
        "cash_available": round(available, 2), "equity": equity, "free_margin": free,
        "margin_per_lot": round(margin_per_lot, 2),
        "max_loss_per_trade_usd": max_loss, "limits": limits,
        "volume": volume, "bound_by": bound[0] if bound else "",
        "margin": cost, "max_loss": loss,
        "flat_by": plan.get("flat_by") or rcfg.get("flat_by"),
        "order_type": order_type, "refusal": refusal,
        "plan": plan,
    }


def bracket_order(s, comment=None):
    """The proposed order, in the pack's own JSON shape."""
    if not s or s.get("refusal") or s.get("volume", 0) <= 0:
        return None
    side = s["side"]
    o = {
        "symbol": s["symbol"], "side": side, "volume": s["volume"], "type": s.get("order_type", "market"),
        "entry": s["entry"], "sl": s["stop"], "tp": s["target"],
        "comment": comment or ("fx-%s-%s" % (s.get("setup") or "trade", s["symbol"]))[:31],
        "why": ("%s %s: %.2f lots, %s at risk at the stop (%.1f pips), %s margin, bound by %s"
                % (side.upper(), s["symbol"], s["volume"], money(s["max_loss"]), s["risk_pips"],
                   money(s["margin"]), s["bound_by"] or "the plan")),
        "plan": {
            "entry": s["entry"], "stop": s["stop"], "target": s["target"],
            "risk_pips": s["risk_pips"], "reward_pips": s["reward_pips"],
            "reward_risk": s["reward_risk"], "max_loss": s["max_loss"],
            "setup": s.get("setup", ""), "session": s.get("session", ""),
            "entry_time": s.get("entry_time", ""), "flat_by": s.get("flat_by"),
            "confidence": (s.get("plan") or {}).get("confidence"),
            "timemachine": (s.get("plan") or {}).get("timemachine"),
        },
    }
    if s.get("order_type") == "limit":
        o["price"] = s["entry"]
    return o
