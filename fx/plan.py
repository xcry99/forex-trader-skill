"""Stop / target arithmetic for long *and* short FX trades.

The original ``trader-exit`` computed a long's stop under the entry and its
target above it, in cents. This is the same function in pips and prices, with
the short side mirrored. The 0.05%-of-price minimum risk is kept: it is what
stops a "stop" from sitting inside the spread.
"""

from .symbols import format_pips

MIN_RISK_PCT = 0.05


class PlanError(Exception):
    pass


def parse_stop(text):
    """or-mid, or-low, or-high, price:X, pips:X, pct:X, atr:K."""
    t = str(text or "").strip().lower().replace("_", "-")
    if t in ("or-mid", "or-low", "or-high"):
        return t.replace("-", "_"), None
    for kind in ("price", "pips", "pct", "atr"):
        prefix = kind + ":"
        if t.startswith(prefix):
            try:
                v = float(t[len(prefix):].replace(",", "").replace("$", ""))
            except ValueError:
                raise PlanError("%s wants a number after the colon (got %r)" % (kind, text))
            if v <= 0:
                raise PlanError("%s needs a number above 0" % kind)
            if kind == "pct" and v >= 100:
                raise PlanError("pct:X needs X under 100")
            return kind, v
    raise PlanError("--stop is or-mid, or-low, or-high, price:X, pips:X, pct:X or atr:K (got %r)" % text)


def parse_target(text):
    """1R, 2R ... or price:X."""
    t = str(text or "").strip().upper()
    if t.endswith("R"):
        try:
            r = float(t[:-1])
        except ValueError:
            raise PlanError("--target needs an R multiple above 0, like 1R or 2R")
        if r <= 0:
            raise PlanError("--target needs an R multiple above 0")
        return "r", r
    if t.startswith("PRICE:"):
        try:
            v = float(t.split(":", 1)[1].replace(",", "").replace("$", ""))
        except ValueError:
            raise PlanError("price:X wants a number")
        return "price", v
    raise PlanError("--target is 2R, 1R, 1.5R ... or price:X (got %r)" % text)


def compute(entry, side, stop_rule, stop_param, target_rule, target_param, spec,
            or_high=None, or_low=None, atr=None, atr_bars=14):
    """Return the plan numbers in prices and pips. Raises PlanError."""
    side = str(side or "").lower()
    if side not in ("buy", "sell"):
        raise PlanError("side must be buy or sell, got %r" % side)
    e = float(entry)
    if e <= 0:
        raise PlanError("the entry price must be above 0")
    long = side == "buy"

    if stop_rule in ("or_mid", "or_low", "or_high"):
        if or_high is None or or_low is None:
            raise PlanError("an %s stop needs the session opening range" % stop_rule.replace("_", "-"))
        oh, ol = float(or_high), float(or_low)
        if oh <= ol:
            raise PlanError("the opening-range high must be above its low")
        if stop_rule == "or_mid":
            raw, label = (oh + ol) / 2.0, "opening-range midpoint"
        elif stop_rule == "or_low":
            raw, label = ol, "opening-range low"
        else:
            raw, label = oh, "opening-range high"
    elif stop_rule == "price":
        raw, label = float(stop_param), "your price"
    elif stop_rule == "pips":
        raw = e - stop_param * spec.pip_size if long else e + stop_param * spec.pip_size
        label = "%s pips %s the entry" % (format_pips(stop_param), "under" if long else "over")
    elif stop_rule == "pct":
        raw = e * (1 - stop_param / 100.0) if long else e * (1 + stop_param / 100.0)
        label = "%s%% %s the entry" % (format_pips(stop_param), "under" if long else "over")
    elif stop_rule == "atr":
        if atr is None:
            raise PlanError("an atr stop needs the session's bars")
        raw = e - stop_param * atr if long else e + stop_param * atr
        label = "%s x ATR(14) %s the entry" % (format_pips(stop_param),
                                               "under" if long else "over")
    else:
        raise PlanError("unknown stop rule %r" % stop_rule)

    stop = spec.round_price(raw)
    if long and stop >= e:
        raise PlanError("the stop %s is at or above the entry %s: a long's stop goes under"
                        % (stop, e))
    if not long and stop <= e:
        raise PlanError("the stop %s is at or below the entry %s: a short's stop goes over"
                        % (stop, e))
    if stop <= 0:
        raise PlanError("the stop works out to %s" % stop)

    risk = abs(e - stop)
    risk_pips = spec.pips(risk)
    floor = e * MIN_RISK_PCT / 100.0
    if risk < floor:
        raise PlanError("the stop is %s away (%.1f pips), less than %.2f%% of the price: inside the "
                        "spread's noise. Widen it." % (spec.round_price(risk), risk_pips, MIN_RISK_PCT))

    if target_rule == "r":
        target = spec.round_price(e + target_param * risk if long else e - target_param * risk)
        t_label = "%sR" % format_pips(target_param)
    else:
        target = spec.round_price(target_param)
        t_label = "your price"
    if long and target <= e:
        raise PlanError("the target %s is at or below the entry %s: a long's target goes above"
                        % (target, e))
    if not long and target >= e:
        raise PlanError("the target %s is at or above the entry %s: a short's target goes below"
                        % (target, e))
    reward = abs(target - e)
    rr = reward / risk if risk else 0.0
    return {
        "side": side,
        "entry": e,
        "stop": stop,
        "target": target,
        "risk_price": risk,
        "reward_price": reward,
        "risk_pips": round(risk_pips, 2),
        "reward_pips": round(spec.pips(reward), 2),
        "reward_risk": round(rr, 2),
        "stop_rule": stop_rule,
        "stop_label": label,
        "target_rule": target_rule,
        "target_label": t_label,
        "atr": atr,
        "atr_pips": round(spec.pips(atr), 2) if atr else None,
        "atr_bars": atr_bars,
        "pip_size": spec.pip_size,
        "digits": spec.digits,
    }
