"""Contract specifications and pip arithmetic.

A pip is not always ``point``:

* 5-digit pairs (EURUSD 1.11391): point 0.00001, pip 0.0001 (10 points).
* 3-digit JPY pairs (USDJPY 158.499): point 0.001, pip 0.01 (10 points).
* 4-digit pairs (EURCZK): point 0.0001, pip 0.0001.
* 2-digit instruments (not FX, but the broker has a few): pip = point.

MetaTrader's ``trade_tick_value`` already converts one tick of one lot into
the account currency, which is why the sizing code prefers
``order_calc_profit`` when a live gateway is available.
"""

import math
from dataclasses import dataclass, asdict


@dataclass
class SymbolSpec:
    name: str
    description: str = ""
    digits: int = 5
    point: float = 0.00001
    contract_size: float = 100000.0
    volume_min: float = 0.01
    volume_max: float = 100.0
    volume_step: float = 0.01
    spread_points: float = 0.0
    trade_stops_level: int = 0
    trade_freeze_level: int = 0
    currency_base: str = ""
    currency_profit: str = ""
    margin_currency: str = ""
    tick_value: float = 0.0
    tick_value_loss: float = 0.0
    trade_mode: int = 4
    filling_mode: int = 0
    path: str = ""
    bid: float = 0.0
    ask: float = 0.0

    @property
    def pip_size(self):
        if self.digits >= 5:
            return self.point * 10.0
        if self.digits == 3:
            return self.point * 10.0
        return self.point

    @property
    def pip_points(self):
        return self.pip_size / self.point if self.point else 1.0

    @property
    def spread_pips(self):
        return self.spread_points / self.pip_points if self.pip_points else 0.0

    @property
    def is_forex(self):
        return (len(self.currency_base or "") == 3 and len(self.currency_profit or "") == 3
                and self.contract_size >= 1000)

    def pips(self, price_delta):
        return price_delta / self.pip_size if self.pip_size else 0.0

    def price_from_pips(self, pips):
        return pips * self.pip_size

    def round_price(self, price):
        return round(float(price), self.digits)

    def round_volume(self, volume, mode="down"):
        """Snap a lot size to the broker's step, inside min/max."""
        step = self.volume_step or 0.01
        v = float(volume)
        if mode == "down":
            v = math.floor(v / step + 1e-9) * step
        else:
            v = round(v / step) * step
        v = max(0.0, min(v, self.volume_max))
        return round(v, _decimals(step))

    def clamp_volume(self, volume):
        """Smallest tradable volume at least ``volume``, or 0 if it cannot be."""
        step = self.volume_step or 0.01
        v = self.round_volume(volume, "up")
        if v + 1e-9 < self.volume_min:
            return 0.0
        return v

    def as_dict(self):
        d = asdict(self)
        d["pip_size"] = self.pip_size
        d["pip_points"] = self.pip_points
        d["spread_pips"] = round(self.spread_pips, 2)
        return d


def _decimals(step):
    s = ("%.10f" % float(step)).rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def spec_from_mt5(info):
    """MetaTrader5.SymbolInfo -> SymbolSpec."""
    return SymbolSpec(
        name=info.name,
        description=getattr(info, "description", "") or "",
        digits=int(info.digits),
        point=float(info.point),
        contract_size=float(info.trade_contract_size),
        volume_min=float(info.volume_min),
        volume_max=float(info.volume_max),
        volume_step=float(info.volume_step),
        spread_points=float(info.spread),
        trade_stops_level=int(getattr(info, "trade_stops_level", 0) or 0),
        trade_freeze_level=int(getattr(info, "trade_freeze_level", 0) or 0),
        currency_base=getattr(info, "currency_base", "") or "",
        currency_profit=getattr(info, "currency_profit", "") or "",
        margin_currency=getattr(info, "currency_margin", "") or "",
        tick_value=float(getattr(info, "trade_tick_value", 0) or 0),
        tick_value_loss=float(getattr(info, "trade_tick_value_loss", 0) or 0),
        trade_mode=int(getattr(info, "trade_mode", 4) or 4),
        filling_mode=int(getattr(info, "filling_mode", 0) or 0),
        path=getattr(info, "path", "") or "",
        bid=float(getattr(info, "bid", 0) or 0),
        ask=float(getattr(info, "ask", 0) or 0),
    )


def spec_from_dict(d):
    keys = {f for f in SymbolSpec.__dataclass_fields__}
    return SymbolSpec(**{k: v for k, v in (d or {}).items() if k in keys})


def alias_candidates(name):
    """EURUSD, eurusd, EURUSD.a, EURUSDm -> a small set of names to try."""
    raw = str(name or "").strip().upper()
    out = [raw]
    for sep in (".", "_", "-", "#"):
        if sep in raw:
            out.append(raw.split(sep)[0])
    if raw.endswith("M") and len(raw) > 6:
        out.append(raw[:-1])
    if len(raw) == 6:
        out.extend([raw + ".A", raw + "M", raw + ".", raw + "_A", raw + "#"])
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def pair_parts(name):
    """'EURUSD' -> ('EUR','USD'); 'EURUSD.a' -> ('EUR','USD'); else (None,None)."""
    raw = str(name or "").strip().upper()
    for sep in (".", "_", "-", "#"):
        if sep in raw:
            raw = raw.split(sep)[0]
    if len(raw) >= 6 and raw[:3].isalpha() and raw[3:6].isalpha():
        return raw[:3], raw[3:6]
    return None, None


def format_price(price, spec):
    return ("%%.%df" % spec.digits) % float(price)


def format_pips(pips):
    # Two decimals, trailing zeros stripped: works for pips (43.4), lot sizes
    # (0.01) and the 0.01 lot step without collapsing a real size to "0".
    return ("%.2f" % float(pips)).rstrip("0").rstrip(".")


def money(x, currency="USD"):
    x = float(x or 0)
    sign = "-" if x < 0 else ""
    return "%s%s%s" % (sign, "$" if currency == "USD" else "", "{:,.2f}".format(abs(x)))
