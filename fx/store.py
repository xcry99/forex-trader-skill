"""Files in ``trader/`` and atomic JSON writes.

Every skill reads what the previous one wrote:

    universe.json -> scan.json -> entry.json -> plan.json -> timemachine.json
    -> orders.json -> state/risk.json -> state/broker.json -> journal.jsonl
"""

import json
import os
import tempfile


def trader_dir(cfg):
    d = ((cfg or {}).get("paths") or {}).get("trader_dir") or os.path.join(os.getcwd(), "trader")
    return os.path.abspath(d)


def ensure(cfg):
    d = trader_dir(cfg)
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    return d


def path(cfg, name):
    d = ensure(cfg)
    if name.startswith("state/"):
        return os.path.join(d, name.replace("/", os.sep))
    return os.path.join(d, name)


def load_json(cfg, name, default=None):
    p = path(cfg, name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        raise ValueError("could not read %s: %s" % (p, e))


def save_json(cfg, name, doc):
    p = path(cfg, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False, default=str)
            fh.write("\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return p


def load_state(cfg, name="broker"):
    return load_json(cfg, "state/%s.json" % name, {}) or {}


def save_state(cfg, name, doc):
    return save_json(cfg, "state/%s.json" % name, doc)


def append_jsonl(cfg, name, record):
    p = path(cfg, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    return p


def read_jsonl(cfg, name):
    p = path(cfg, name)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def write_jsonl(cfg, name, records):
    p = path(cfg, name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(p))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return p
