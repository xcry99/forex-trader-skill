"""Shared command-line plumbing: config, clock, gateway, output."""

import argparse
import json
import sys

from . import config as config_mod, store, timeutil


def add_common(p, now=True):
    p.add_argument("--config", help="trader/config.json (default: ./trader/config.json)")
    p.add_argument("--trader-dir", help="folder for the runtime JSON files (default: ./trader)")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    if now:
        p.add_argument("--now", help='pin the clock, New York time: "2026-10-19 10:15" or UTC with Z')


def cfg_from(args):
    return config_mod.load_config(getattr(args, "config", None), getattr(args, "trader_dir", None))


def now_from(args, cfg=None):
    text = getattr(args, "now", None)
    if text:
        return timeutil.parse_now(text)
    return timeutil.utc_now()


def gateway_from(cfg, connect=True):
    from .gateway import MT5Gateway
    return MT5Gateway(cfg, connect=connect)


def emit(doc, args, text):
    if getattr(args, "json", False):
        print(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
    else:
        print(text)


def table(rows, headers, aligns=None):
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    cells = []
    for r in rows:
        row = [str(x if x is not None else "") for x in r]
        cells.append(row)
        for i, x in enumerate(row):
            widths[i] = max(widths[i], len(x))
    aligns = aligns or ["<"] * len(headers)
    out = ["  ".join(("%" + ("-" if aligns[i] == "<" else "") + str(widths[i]) + "s") % headers[i]
                     for i in range(len(headers)))]
    out.append("  ".join("-" * w for w in widths))
    for row in cells:
        out.append("  ".join(("%" + ("-" if aligns[i] == "<" else "") + str(widths[i]) + "s") % row[i]
                             for i in range(len(headers))))
    return "\n".join(out)


def load_orders(cfg):
    return store.load_json(cfg, "orders.json", []) or []


def fail(msg, code=2):
    sys.stderr.write(str(msg).rstrip() + "\n")
    return code


def require_gateway(cfg):
    from .gateway import GatewayError
    try:
        return gateway_from(cfg)
    except GatewayError as e:
        raise SystemExit(str(e))
