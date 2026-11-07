"""Config defaults and deep merge.

The config lives in ``trader/config.json`` next to the runtime files. Every
skill takes ``--config`` so several accounts can coexist.
"""

import copy
import json
import os

DEFAULT_CONFIG = {
    "account": {
        # The budget the agent sizes against. The original pack had a $5,000
        # budget and a $50 max loss (1%). This experiment is a $1,000 demo,
        # so the same 1% rule is $10 a trade.
        "budget": 1000.0,
        "currency": "USD",
        "leverage": 1000,
        "starting_balance": 1000.0,
        "mode": "demo",
        "auto_execute": True,
    },
    "mt5": {
        # Leave null to use the terminal that is already logged in.
        "login": None,
        "password": None,
        "server": None,
        "path": None,
        "server_utc_offset": None,   # null = auto (EET rule / live tick)
        "magic": 20261107,
        "deviation_points": 20,
        "timeout_ms": 60000,
        "filling": None,             # null = pick from the symbol's filling_mode
    },
    "risk": {
        # The original trader-risk rules, translated to lots and margin.
        "cash_buffer_pct": 10,
        "max_margin_pct": 75,        # was max_position_pct of the account
        "max_order_lots": 10.0,      # was max_order_usd
        "min_order_lots": 0.01,      # was min_order_usd
        "max_loss_per_trade_usd": 10.0,
        "max_daily_loss_usd": 30.0,
        "max_trades_per_day": 3,
        "max_orders_per_day": 10,
        "min_reward_risk": 1.0,
        "require_stop": True,
        "allow_short": True,
        "allow_margin": True,
        "blocked": [],
        "no_entries_before": "02:30",   # NY time; before the London open
        "no_entries_after": "11:30",    # NY time; the London/NY overlap is the liquid part
        "flat_by": "16:55",             # NY time; Friday the week closes at 17:00
        "max_spread_pips": None,        # optional; null = do not block on spread
        "pdt_guard": False,             # no pattern-day-trader rule in FX; kept for parity
    },
    "strategy": {
        "watchlist": [
            "EURUSD", "GBPUSD", "USDJPY", "USDCHF", "USDCAD", "AUDUSD", "NZDUSD",
            "EURGBP", "EURJPY", "GBPJPY", "AUDJPY", "EURAUD", "EURNZD", "GBPCHF",
        ],
        "min_daily_bars": 30,
        "setup_min_score": 1,
        "orb": {
            "session": "london",
            "or_minutes": 30,
            "confirm": "close",       # close = first completed M5 close beyond the range
            "interval": "M5",
        },
        "timemachine": {
            "replays": 10000,
            "seed": 19,
            "window": 2,
            "days": 60,
        },
        "news": {
            "hours": 24,
            "avoid_high_impact_minutes": 0,   # 0 = news is context, not a gate
            "currencies": None,
        },
        "scan": {
            "max_candidates": 6,
            "daily_top": 20,
        },
    },
    "news": {
        "feeds": [
            "https://www.forexlive.com/feed/news",
            "https://www.fxstreet.com/rss/news",
            "https://www.dailyfx.com/feeds/market-news",
        ],
        "user_agent": "forex-trader-skill/1.0 (+https://github.com/Jakeschincariol/day-trader-skill)",
    },
    "alerts": {
        "poll_seconds": 15,
        "execute_on_stop": False,     # MT5 already holds the bracket server-side
    },
    "journal": {
        "starting_balance": 1000.0,
    },
    "paths": {
        "trader_dir": None,           # null = ./trader next to the skills
    },
}


class ConfigError(Exception):
    pass


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path=None, trader_dir=None):
    """Read config.json and merge it over the defaults.

    ``trader_dir`` may also come from ``FOREX_TRADER_DIR``. A missing file is
    fine: the defaults are a working $1,000 demo setup.
    """
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError) as e:
            raise ConfigError("could not read %s: %s" % (path, e))
        if not isinstance(raw, dict):
            raise ConfigError("%s must hold a JSON object" % path)
    else:
        raw = {}
    cfg = _merge(DEFAULT_CONFIG, raw)
    td = (trader_dir or cfg.get("paths", {}).get("trader_dir")
          or os.environ.get("FOREX_TRADER_DIR") or "trader")
    cfg.setdefault("paths", {})["trader_dir"] = os.path.abspath(td)
    cfg["_source"] = os.path.abspath(path) if path and os.path.exists(path) else None
    return cfg


def save_example(path):
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["paths"]["trader_dir"] = "trader"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=False)
        fh.write("\n")
    return path


def risk_cfg(cfg):
    return cfg.get("risk") or {}


def account_cfg(cfg):
    return cfg.get("account") or {}


def strategy_cfg(cfg):
    return cfg.get("strategy") or {}
