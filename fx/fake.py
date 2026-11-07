"""In-memory MetaTrader lookalike for tests and ``--offline`` runs.

It implements the same surface as :class:`fx.gateway.MT5Gateway` but keeps
everything in dictionaries. Prices, fills and P&L are simulated with the
symbol's own contract specification, so the sizing and risk code exercises
the same arithmetic it uses live.
"""

import datetime
import itertools

from .symbols import SymbolSpec, pair_parts
from . import timeutil


DEFAULT_SPECS = {
    "EURUSD": SymbolSpec("EURUSD", "Euro vs US Dollar", 5, 0.00001, 100000, 0.01, 100, 0.01,
                         spread_points=16, currency_base="EUR", currency_profit="USD",
                         margin_currency="EUR", tick_value=1.0, tick_value_loss=1.0, filling_mode=3,
                         bid=1.11391, ask=1.11407),
    "GBPUSD": SymbolSpec("GBPUSD", "Great Britain Pound vs US Dollar", 5, 0.00001, 100000, 0.01, 100, 0.01,
                         spread_points=19, currency_base="GBP", currency_profit="USD",
                         margin_currency="GBP", tick_value=1.0, tick_value_loss=1.0, filling_mode=3,
                         bid=1.33512, ask=1.33531),
    "USDJPY": SymbolSpec("USDJPY", "US Dollar vs Japanese Yen", 3, 0.001, 100000, 0.01, 100, 0.01,
                         spread_points=24, currency_base="USD", currency_profit="JPY",
                         margin_currency="USD", tick_value=0.6308, tick_value_loss=0.6309, filling_mode=3,
                         bid=158.499, ask=158.523),
    "AUDUSD": SymbolSpec("AUDUSD", "Australian Dollar vs US Dollar", 5, 0.00001, 100000, 0.01, 100, 0.01,
                         spread_points=17, currency_base="AUD", currency_profit="USD",
                         margin_currency="AUD", tick_value=1.0, tick_value_loss=1.0, filling_mode=3,
                         bid=0.68657, ask=0.68674),
}


class FakeGateway(object):
    def __init__(self, account=None, specs=None, bars=None, ticks=None, offset_hours=2.0,
                 start_utc=None):
        self.cfg = {"mt5": {"server_utc_offset": offset_hours}}
        self._offset = float(offset_hours)
        self._connected = True
        self._specs = dict(specs or DEFAULT_SPECS)
        self._bars = {}
        for key, rows in (bars or {}).items():
            sym, tf = key if isinstance(key, tuple) else (key, "M5")
            self._bars[(sym.upper(), tf.upper())] = [self._norm_bar(b) for b in rows]
        self._ticks = {k.upper(): dict(v) for k, v in (ticks or {}).items()}
        self._account = {
            "login": 10000001, "trade_mode": 0, "trade_mode_name": "demo", "is_demo": True,
            "leverage": 1000, "balance": 1000.0, "equity": 1000.0, "margin": 0.0,
            "margin_free": 1000.0, "margin_level": 0.0, "profit": 0.0, "credit": 0.0,
            "currency": "USD", "server": "Fake-Demo", "company": "Fake Broker",
            "name": "Fake Account", "trade_allowed": True, "trade_expert": True,
        }
        if account:
            self._account.update(account)
        self._positions = []
        self._orders = []
        self._deals = []
        self._tickets = itertools.count(500001)
        self._position_ids = itertools.count(900001)
        self.start_utc = start_utc or timeutil.utc_now()

    # ------------------------------------------------------------- helpers

    def _norm_bar(self, b):
        t = int(b.get("t") or 0)
        t_utc = b.get("t_utc")
        if isinstance(t_utc, str):
            t_utc = timeutil.parse_utc(t_utc)
        if t_utc is None:
            t_utc = timeutil.bar_utc(t, self._offset) if t else self.start_utc
        return {"t": t or timeutil.utc_epoch(t_utc, self._offset), "t_utc": t_utc,
                "o": float(b["o"]), "h": float(b["h"]), "l": float(b["l"]), "c": float(b["c"]),
                "v": float(b.get("v") or 0), "real_volume": float(b.get("real_volume") or 0),
                "spread": int(b.get("spread") or 0)}

    def connect(self):
        self._connected = True
        return True

    def shutdown(self):
        self._connected = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()

    @property
    def connected(self):
        return self._connected

    @property
    def offset(self):
        return self._offset

    # ------------------------------------------------------------- account

    def account(self):
        d = dict(self._account)
        d["equity"] = round(d["balance"] + sum(p["profit"] for p in self._positions), 2)
        d["margin"] = round(sum(self._position_margin(p) for p in self._positions), 2)
        d["margin_free"] = round(d["equity"] - d["margin"], 2)
        d["margin_level"] = round(100.0 * d["equity"] / d["margin"], 2) if d["margin"] else 0.0
        d["profit"] = round(sum(p["profit"] for p in self._positions), 2)
        return d

    def terminal(self):
        return {"connected": self._connected, "trade_allowed": True, "tradeapi_disabled": False,
                "path": "fake", "data_path": "fake", "build": 0, "algo_trading": True}

    # ------------------------------------------------------------- symbols

    def symbols(self):
        return list(self._specs.values())

    def symbol(self, name):
        key = str(name or "").strip().upper()
        if key in self._specs:
            return self._specs[key]
        base, quote = pair_parts(key)
        for s in self._specs.values():
            if s.currency_base == base and s.currency_profit == quote:
                return s
        raise KeyError("the broker does not offer %s" % name)

    def resolve(self, name):
        return self.symbol(name).name

    def set_price(self, name, bid, ask=None):
        spec = self.symbol(name)
        ask = ask if ask is not None else bid + spec.spread_points * spec.point
        self._ticks[spec.name] = {"bid": float(bid), "ask": float(ask), "last": float(bid),
                                  "time_epoch": timeutil.utc_epoch(timeutil.utc_now(), self._offset)}
        spec.bid, spec.ask = float(bid), float(ask)
        self._mark_positions()

    # ------------------------------------------------------------- market data

    def tick(self, name):
        spec = self.symbol(name)
        t = self._ticks.get(spec.name)
        if t is None:
            rows = self._bars.get((spec.name, "M5")) or []
            px = rows[-1]["c"] if rows else (spec.bid or 1.0)
            half = spec.spread_points * spec.point / 2.0
            t = {"bid": px - half, "ask": px + half, "last": px,
                 "time_epoch": rows[-1]["t"] if rows else timeutil.utc_epoch(self.start_utc, self._offset)}
            self._ticks[spec.name] = t
        return {"symbol": spec.name, "bid": float(t["bid"]), "ask": float(t["ask"]),
                "last": float(t.get("last") or t["bid"]), "mid": (t["bid"] + t["ask"]) / 2.0,
                "time_epoch": int(t.get("time_epoch") or 0),
                "time_utc": timeutil.bar_utc(t.get("time_epoch") or 0, self._offset),
                "spread_points": (t["ask"] - t["bid"]) / spec.point,
                "spread_pips": (t["ask"] - t["bid"]) / spec.pip_size,
                "digits": spec.digits, "pip_size": spec.pip_size}

    def rates(self, name, timeframe="M5", count=500, start_pos=0):
        spec = self.symbol(name)
        rows = self._bars.get((spec.name, str(timeframe).upper())) or []
        if not rows:
            raise KeyError("no %s bars for %s" % (timeframe, spec.name))
        end = len(rows) - int(start_pos)
        return rows[max(0, end - int(count)):end]

    def rates_range(self, name, timeframe, from_utc, to_utc):
        rows = self.rates(name, timeframe, 100000)
        return [b for b in rows if from_utc <= b["t_utc"] <= to_utc]

    # ------------------------------------------------------------- maths

    def calc_profit(self, side, name, volume, price_open, price_close):
        spec = self.symbol(name)
        direction = 1.0 if str(side).lower() == "buy" else -1.0
        delta = (float(price_close) - float(price_open)) * direction
        per_point = spec.tick_value_loss or spec.tick_value or (spec.contract_size * spec.point)
        return delta / spec.point * per_point * float(volume)

    def calc_margin(self, side, name, volume, price):
        spec = self.symbol(name)
        lev = float(self._account.get("leverage") or 1000)
        notional_quote = float(volume) * spec.contract_size * float(price)
        if spec.currency_profit == "USD":
            notional = notional_quote
        elif spec.currency_base == "USD":
            notional = float(volume) * spec.contract_size
        else:
            notional = notional_quote  # fixture approximation
        return notional / lev

    def risk_per_lot(self, side, name, entry, stop):
        return abs(self.calc_profit(side, name, 1.0, entry, stop))

    # ------------------------------------------------------------- positions

    def positions(self, symbol=None):
        self._mark_positions()
        rows = [dict(p) for p in self._positions]
        if symbol:
            rows = [p for p in rows if p["symbol"] == self.symbol(symbol).name]
        return rows

    def orders(self):
        return [dict(o) for o in self._orders]

    def _position_margin(self, p):
        return self.calc_margin(p["side"], p["symbol"], p["volume"], p["price_open"])

    def _mark_positions(self):
        for p in list(self._positions):
            t = self.tick(p["symbol"])
            px = t["bid"] if p["side"] == "buy" else t["ask"]
            p["profit"] = round(self.calc_profit(p["side"], p["symbol"], p["volume"], p["price_open"], px), 2)
            p["current_price"] = px
            if p.get("sl"):
                if (p["side"] == "buy" and px <= p["sl"]) or (p["side"] == "sell" and px >= p["sl"]):
                    self.close_position(p, comment="sl")
                    continue
            if p.get("tp"):
                if (p["side"] == "buy" and px >= p["tp"]) or (p["side"] == "sell" and px <= p["tp"]):
                    self.close_position(p, comment="tp")

    def open_order(self, name, side, volume, sl=None, tp=None, comment="", magic=20261107, price=None):
        spec = self.symbol(name)
        side = str(side).lower()
        t = self.tick(spec.name)
        px = float(price) if price else (t["ask"] if side == "buy" else t["bid"])
        ticket = next(self._tickets)
        pid = next(self._position_ids)
        pos = {"ticket": ticket, "position_id": pid, "symbol": spec.name,
               "type": 0 if side == "buy" else 1, "side": side, "volume": float(volume),
               "price_open": px, "sl": float(sl or 0), "tp": float(tp or 0),
               "profit": 0.0, "swap": 0.0, "commission": 0.0,
               "time": timeutil.utc_epoch(timeutil.utc_now(), self._offset),
               "time_utc": timeutil.utc_now(), "magic": int(magic), "comment": comment or "",
               "current_price": px}
        self._positions.append(pos)
        self._deals.append({"ticket": ticket, "position_id": pid, "symbol": spec.name,
                            "type": pos["type"], "entry": 0, "volume": float(volume), "price": px,
                            "profit": 0.0, "commission": 0.0, "swap": 0.0,
                            "time": pos["time"], "time_utc": pos["time_utc"],
                            "magic": int(magic), "comment": comment or ""})
        return {"ok": True, "retcode": 10009, "order": ticket, "deal": ticket,
                "volume": float(volume), "price": px, "comment": "done", "position": ticket}

    def close_position(self, position, volume=None, comment="close", price=None):
        p = position
        if not isinstance(p, dict):
            p = next((x for x in self._positions if x["ticket"] == int(position)), None)
            if p is None:
                raise KeyError("position %s not found" % position)
        t = self.tick(p["symbol"])
        px = float(price) if price else (t["bid"] if p["side"] == "buy" else t["ask"])
        vol = float(volume or p["volume"])
        profit = self.calc_profit(p["side"], p["symbol"], vol, p["price_open"], px)
        self._account["balance"] = round(self._account["balance"] + profit, 2)
        self._deals.append({"ticket": next(self._tickets), "position_id": p["position_id"],
                            "symbol": p["symbol"], "type": 0 if p["side"] == "buy" else 1,
                            "entry": 1, "volume": vol, "price": px, "profit": round(profit, 2),
                            "commission": 0.0, "swap": 0.0,
                            "time": timeutil.utc_epoch(timeutil.utc_now(), self._offset),
                            "time_utc": timeutil.utc_now(), "magic": p.get("magic", 0),
                            "comment": comment})
        p["volume"] = round(p["volume"] - vol, 2)
        if p["volume"] <= 1e-9:
            self._positions.remove(p)
        return {"ok": True, "retcode": 10009, "order": p["ticket"], "deal": p["ticket"],
                "volume": vol, "price": px, "comment": "done", "profit": round(profit, 2)}

    def modify_position(self, ticket, sl=None, tp=None):
        p = next((x for x in self._positions if x["ticket"] == int(ticket)), None)
        if p is None:
            raise KeyError("position %s not found" % ticket)
        if sl is not None:
            p["sl"] = float(sl)
        if tp is not None:
            p["tp"] = float(tp)
        return {"ok": True, "retcode": 10009, "comment": "done"}

    def order_check(self, request):
        return {"retcode": 0, "comment": "ok", "balance": self._account["balance"],
                "equity": self.account()["equity"], "margin": 0.0, "margin_free": self.account()["margin_free"]}

    def order_send(self, request):
        action = request.get("action")
        if action == 6:  # SLTP
            return self.modify_position(request["position"], request.get("sl"), request.get("tp"))
        if request.get("position"):
            return self.close_position(request["position"], request.get("volume"),
                                       request.get("comment", "close"), request.get("price"))
        side = "buy" if request.get("type") == 0 else "sell"
        return self.open_order(request["symbol"], side, request["volume"], request.get("sl"),
                               request.get("tp"), request.get("comment"), request.get("magic"),
                               request.get("price"))

    # ------------------------------------------------------------- history

    def deals(self, from_utc, to_utc):
        return [dict(d) for d in self._deals if from_utc <= d["time_utc"] <= to_utc]

    def account_summary(self):
        a = self.account()
        return {"login": a["login"], "server": a["server"], "currency": a["currency"],
                "balance": a["balance"], "equity": a["equity"], "margin": a["margin"],
                "margin_free": a["margin_free"], "margin_level": a["margin_level"],
                "leverage": a["leverage"], "profit": a["profit"],
                "open_positions": len(self._positions), "is_demo": True, "trade_mode": "demo"}
