"""MetaTrader 5 gateway.

The only module that imports ``MetaTrader5``. Everything else talks to this
interface, which is what lets the whole pack run against a fixture in tests
(:class:`fx.fake.FakeGateway`).

Prices coming out of MetaTrader are stamped in *server* time. Every bar and
tick therefore carries both ``t`` (the raw server epoch) and ``t_utc`` (the
true UTC instant), computed with :func:`fx.timeutil.bar_utc`.
"""

import datetime
import glob
import os
import subprocess
import time

from . import timeutil
from .symbols import SymbolSpec, spec_from_mt5, alias_candidates, pair_parts

TF_NAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")


class GatewayError(Exception):
    pass


def _as_dict(obj):
    if obj is None:
        return None
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    return dict(obj)


class MT5Gateway(object):
    def __init__(self, cfg=None, connect=True):
        self.cfg = cfg or {}
        self._mt5 = None
        self._connected = False
        self._specs = {}
        self._offset = timeutil.server_offset_hours(self.cfg)
        self._terminal_path = None
        if connect:
            self.connect()

    # ------------------------------------------------------------- lifecycle

    def _import(self):
        try:
            import MetaTrader5 as mt5
        except ImportError:
            raise GatewayError(
                "the MetaTrader5 package is not installed. Run: pip install MetaTrader5 "
                "(Windows only, Python 3.8+). The MT5 terminal must also be running."
            )
        self._mt5 = mt5
        return mt5

    def _find_terminal(self):
        mcfg = self.cfg.get("mt5") or {}
        if mcfg.get("path"):
            p = mcfg["path"]
            if os.path.isdir(p):
                p = os.path.join(p, "terminal64.exe")
            return p if os.path.exists(p) else None
        patterns = [
            r"C:\Program Files\*\terminal64.exe",
            r"C:\Program Files (x86)\*\terminal64.exe",
            os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes", "Terminal", "*", "terminal64.exe"),
        ]
        for pat in patterns:
            hits = sorted(glob.glob(pat))
            if hits:
                # Prefer a terminal whose folder mentions MetaTrader.
                hits.sort(key=lambda p: ("metatrader" not in p.lower(), p))
                return hits[0]
        return None

    def connect(self):
        mt5 = self._import()
        mcfg = self.cfg.get("mt5") or {}
        kwargs = {"timeout": int(mcfg.get("timeout_ms") or 60000)}
        for key in ("login", "password", "server", "path"):
            if mcfg.get(key) not in (None, ""):
                kwargs[key] = mcfg[key]
        if "path" in kwargs and os.path.isdir(kwargs["path"]):
            kwargs["path"] = os.path.join(kwargs["path"], "terminal64.exe")
        ok = mt5.initialize(**kwargs)
        if not ok:
            err = mt5.last_error()
            # Try to start the terminal once, then retry.
            exe = self._find_terminal()
            if exe and not mt5.terminal_info():
                try:
                    subprocess.Popen([exe], close_fds=True)
                except OSError:
                    exe = None
                if exe:
                    for _ in range(30):
                        time.sleep(1)
                        if mt5.initialize(**kwargs):
                            ok = True
                            break
            if not ok:
                raise GatewayError(
                    "could not connect to MetaTrader 5 (%s). Open the terminal, log in to the "
                    "demo account, and enable Tools > Options > Expert Advisors > Allow Algo Trading."
                    % (err,)
                )
        self._connected = True
        self._offset = timeutil.detect_server_offset(self, self.cfg)
        ai = mt5.account_info()
        if ai is None:
            raise GatewayError("connected to the terminal but no account is logged in")
        return True

    def shutdown(self):
        if self._mt5 and self._connected:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
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

    @property
    def mt5(self):
        if self._mt5 is None:
            self._import()
        return self._mt5

    # ------------------------------------------------------------- account

    def account(self):
        ai = self.mt5.account_info()
        if ai is None:
            raise GatewayError("no account is logged in")
        d = _as_dict(ai)
        d["trade_mode_name"] = {0: "demo", 1: "contest", 2: "real"}.get(d.get("trade_mode"), "unknown")
        d["is_demo"] = d.get("trade_mode") == 0
        return d

    def terminal(self):
        ti = self.mt5.terminal_info()
        if ti is None:
            raise GatewayError("the MetaTrader terminal is not available")
        d = _as_dict(ti)
        d["algo_trading"] = bool(d.get("trade_allowed"))
        d["tradeapi_disabled"] = bool(d.get("tradeapi_disabled"))
        return d

    # ------------------------------------------------------------- symbols

    def symbols(self):
        raw = self.mt5.symbols_get()
        if raw is None:
            raise GatewayError("could not list symbols: %s" % (self.mt5.last_error(),))
        out = []
        for info in raw:
            try:
                out.append(spec_from_mt5(info))
            except Exception:
                continue
        return out

    def symbol(self, name):
        key = str(name or "").strip().upper()
        if key in self._specs:
            return self._specs[key]
        mt5 = self.mt5
        found = None
        for cand in alias_candidates(key):
            info = mt5.symbol_info(cand)
            if info is not None:
                found = info
                break
        if found is None:
            base, quote = pair_parts(key)
            if base and quote:
                for info in mt5.symbols_get() or []:
                    if (getattr(info, "currency_base", "") == base
                            and getattr(info, "currency_profit", "") == quote):
                        found = info
                        break
        if found is None:
            raise GatewayError("the broker does not offer %s" % name)
        if not found.visible:
            mt5.symbol_select(found.name, True)
            found = mt5.symbol_info(found.name) or found
        spec = spec_from_mt5(found)
        self._specs[key] = spec
        self._specs[spec.name.upper()] = spec
        return spec

    def resolve(self, name):
        return self.symbol(name).name

    # ------------------------------------------------------------- market data

    def tick(self, name):
        spec = self.symbol(name)
        t = self.mt5.symbol_info_tick(spec.name)
        if t is None:
            raise GatewayError("no tick for %s (market closed or symbol not selected)" % spec.name)
        spread_points = (t.ask - t.bid) / spec.point if spec.point else 0.0
        return {
            "symbol": spec.name,
            "bid": float(t.bid),
            "ask": float(t.ask),
            "last": float(t.last),
            "mid": (float(t.bid) + float(t.ask)) / 2.0,
            "time_epoch": int(t.time),
            "time_utc": timeutil.bar_utc(t.time, self._offset),
            "spread_points": spread_points,
            "spread_pips": spread_points / spec.pip_points if spec.pip_points else 0.0,
            "digits": spec.digits,
            "pip_size": spec.pip_size,
        }

    def _tf(self, timeframe):
        name = str(timeframe or "M5").upper()
        if name not in TF_NAMES:
            raise GatewayError("unknown timeframe %r (use %s)" % (timeframe, ", ".join(TF_NAMES)))
        return getattr(self.mt5, "TIMEFRAME_" + name)

    def rates(self, name, timeframe="M5", count=500, start_pos=0):
        spec = self.symbol(name)
        raw = self.mt5.copy_rates_from_pos(spec.name, self._tf(timeframe), int(start_pos), int(count))
        if raw is None:
            raise GatewayError("no %s bars for %s: %s" % (timeframe, spec.name, self.mt5.last_error()))
        return [self._bar(r) for r in raw]

    def rates_range(self, name, timeframe, from_utc, to_utc):
        spec = self.symbol(name)
        a = timeutil.utc_epoch(from_utc, self._offset)
        b = timeutil.utc_epoch(to_utc, self._offset)
        raw = self.mt5.copy_rates_range(spec.name, self._tf(timeframe), a, b)
        if raw is None:
            raise GatewayError("no %s bars for %s in that range" % (timeframe, spec.name))
        return [self._bar(r) for r in raw]

    def _bar(self, r):
        return {
            "t": int(r["time"]),
            "t_utc": timeutil.bar_utc(r["time"], self._offset),
            "o": float(r["open"]),
            "h": float(r["high"]),
            "l": float(r["low"]),
            "c": float(r["close"]),
            "v": float(r["tick_volume"]),
            "real_volume": float(r["real_volume"]),
            "spread": int(r["spread"]),
        }

    # ------------------------------------------------------------- positions

    def positions(self, symbol=None):
        raw = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        if raw is None:
            return []
        out = []
        for p in raw:
            d = _as_dict(p)
            d["side"] = "buy" if d.get("type") == 0 else "sell"
            d["time_utc"] = timeutil.bar_utc(d.get("time", 0), self._offset)
            out.append(d)
        return out

    def orders(self):
        raw = self.mt5.orders_get()
        if raw is None:
            return []
        out = []
        for o in raw:
            d = _as_dict(o)
            d["side"] = "buy" if d.get("type") in (0, 2, 4, 6) else "sell"
            d["time_utc"] = timeutil.bar_utc(d.get("time_setup", 0), self._offset)
            out.append(d)
        return out

    def deals(self, from_utc, to_utc):
        a = timeutil.utc_epoch(from_utc, self._offset)
        b = timeutil.utc_epoch(to_utc, self._offset)
        raw = self.mt5.history_deals_get(a, b)
        if raw is None:
            return []
        out = []
        for d in raw:
            x = _as_dict(d)
            x["time_utc"] = timeutil.bar_utc(x.get("time", 0), self._offset)
            out.append(x)
        return out

    # ------------------------------------------------------------- order maths

    def calc_profit(self, side, name, volume, price_open, price_close):
        spec = self.symbol(name)
        action = self.mt5.ORDER_TYPE_BUY if str(side).lower() == "buy" else self.mt5.ORDER_TYPE_SELL
        v = self.mt5.order_calc_profit(action, spec.name, float(volume), float(price_open), float(price_close))
        return None if v is None else float(v)

    def calc_margin(self, side, name, volume, price):
        spec = self.symbol(name)
        action = self.mt5.ORDER_TYPE_BUY if str(side).lower() == "buy" else self.mt5.ORDER_TYPE_SELL
        v = self.mt5.order_calc_margin(action, spec.name, float(volume), float(price))
        return None if v is None else float(v)

    def risk_per_lot(self, side, name, entry, stop):
        """Money at risk for one lot from ``entry`` to ``stop``, in account currency."""
        v = self.calc_profit(side, name, 1.0, entry, stop)
        if v is None:
            spec = self.symbol(name)
            return abs(entry - stop) / spec.point * spec.tick_value_loss
        return abs(v)

    # ------------------------------------------------------------- orders

    def _filling(self, spec):
        mcfg = self.cfg.get("mt5") or {}
        if mcfg.get("filling") is not None:
            return int(mcfg["filling"])
        mode = int(spec.filling_mode or 0)
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        return self.mt5.ORDER_FILLING_RETURN

    def _request_base(self, spec, side, volume, price, sl, tp, comment, magic):
        typ = self.mt5.ORDER_TYPE_BUY if side == "buy" else self.mt5.ORDER_TYPE_SELL
        req = {
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": spec.name,
            "volume": float(volume),
            "type": typ,
            "price": float(price),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "deviation": int((self.cfg.get("mt5") or {}).get("deviation_points") or 20),
            "magic": int(magic),
            "comment": (comment or "forex-trader")[:31],
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling(spec),
        }
        return req

    def order_check(self, request):
        return _as_dict(self.mt5.order_check(request))

    def order_send(self, request):
        res = self.mt5.order_send(request)
        if res is None:
            raise GatewayError("order_send returned nothing: %s" % (self.mt5.last_error(),))
        d = _as_dict(res)
        d["ok"] = d.get("retcode") in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED,
                                        self.mt5.TRADE_RETCODE_DONE_PARTIAL)
        return d

    def open_order(self, name, side, volume, sl=None, tp=None, comment="", magic=None, price=None):
        """Market order with an attached bracket. Retries a requote twice."""
        spec = self.symbol(name)
        side = str(side).lower()
        magic = magic if magic is not None else int((self.cfg.get("mt5") or {}).get("magic") or 20261107)
        last = None
        for attempt in range(3):
            t = self.tick(spec.name)
            px = float(price) if price else (t["ask"] if side == "buy" else t["bid"])
            req = self._request_base(spec, side, volume, px, sl, tp, comment, magic)
            chk = self.order_check(req)
            if chk and chk.get("retcode") not in (0, self.mt5.TRADE_RETCODE_DONE):
                last = chk
            res = self.order_send(req)
            if res.get("ok"):
                res["request"] = req
                return res
            last = res
            if res.get("retcode") not in (self.mt5.TRADE_RETCODE_REQUOTE,
                                          self.mt5.TRADE_RETCODE_PRICE_CHANGED,
                                          self.mt5.TRADE_RETCODE_PRICE_OFF):
                break
            time.sleep(0.4 * (attempt + 1))
        raise GatewayError("order rejected: %s (retcode %s)"
                           % ((last or {}).get("comment"), (last or {}).get("retcode")))

    def close_position(self, position, volume=None, comment="close", price=None):
        p = position
        if not isinstance(p, dict):
            found = self.mt5.positions_get(ticket=int(position))
            if not found:
                raise GatewayError("position %s not found" % position)
            p = _as_dict(found[0])
            p["side"] = "buy" if p.get("type") == 0 else "sell"
        spec = self.symbol(p["symbol"])
        vol = float(volume or p.get("volume"))
        side = "sell" if p.get("side") == "buy" or p.get("type") == 0 else "buy"
        t = self.tick(spec.name)
        px = float(price) if price else (t["bid"] if side == "sell" else t["ask"])
        magic = int((self.cfg.get("mt5") or {}).get("magic") or 20261107)
        req = self._request_base(spec, side, vol, px, 0, 0, comment, magic)
        req["position"] = int(p["ticket"])
        res = self.order_send(req)
        if not res.get("ok"):
            raise GatewayError("could not close %s: %s (retcode %s)"
                               % (p["symbol"], res.get("comment"), res.get("retcode")))
        res["request"] = req
        return res

    def modify_position(self, ticket, sl=None, tp=None):
        found = self.mt5.positions_get(ticket=int(ticket))
        if not found:
            raise GatewayError("position %s not found" % ticket)
        p = _as_dict(found[0])
        spec = self.symbol(p["symbol"])
        req = {
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": spec.name,
            "position": int(ticket),
            "sl": float(sl if sl is not None else p.get("sl") or 0.0),
            "tp": float(tp if tp is not None else p.get("tp") or 0.0),
            "magic": int((self.cfg.get("mt5") or {}).get("magic") or 20261107),
        }
        res = self.order_send(req)
        if not res.get("ok"):
            raise GatewayError("could not modify %s: %s" % (ticket, res.get("comment")))
        return res

    def account_summary(self):
        ai = self.account()
        pos = self.positions()
        return {
            "login": ai.get("login"),
            "server": ai.get("server"),
            "currency": ai.get("currency"),
            "balance": ai.get("balance"),
            "equity": ai.get("equity"),
            "margin": ai.get("margin"),
            "margin_free": ai.get("margin_free"),
            "margin_level": ai.get("margin_level"),
            "leverage": ai.get("leverage"),
            "profit": ai.get("profit"),
            "open_positions": len(pos),
            "is_demo": ai.get("is_demo"),
            "trade_mode": ai.get("trade_mode_name"),
        }
