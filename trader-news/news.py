#!/usr/bin/env python3
"""FX news monitor: headlines and event risk by currency.

    python news.py fetch --hours 24 --write
    python news.py fetch --currencies USD,EUR --json
    python news.py check --currency USD

Free public RSS feeds (ForexLive, FXStreet, DailyFX by default). Every item
is tagged with the currencies it mentions and with an impact level from the
event words that actually move FX: rate decisions, CPI, NFP, GDP, PMIs,
central-bank speakers.

News is context, not a gate. ``strategy.news.avoid_high_impact_minutes`` can
turn a high-impact event into a blackout window if you want one; it is 0
(off) by default.
"""

import argparse
import datetime
import email.utils
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fx import cli, store, timeutil

CURRENCIES = {
    "USD": ["usd", "dollar", "fed", "fomc", "powell", "treasury", "non-farm", "nfp"],
    "EUR": ["eur", "euro", "ecb", "lagarde", "eurozone", "germany", "german"],
    "GBP": ["gbp", "pound", "sterling", "boe", "bank of england", "bailey", "uk "],
    "JPY": ["jpy", "yen", "boj", "bank of japan", "ueda", "japan"],
    "CHF": ["chf", "franc", "snb", "swiss"],
    "CAD": ["cad", "loonie", "boc", "bank of canada", "canada"],
    "AUD": ["aud", "aussie", "rba", "reserve bank of australia", "australia"],
    "NZD": ["nzd", "kiwi", "rbnz", "new zealand"],
}

HIGH = [
    "fomc", "rate decision", "interest rate", "rate hike", "rate cut", "cpi", "inflation",
    "non-farm", "nonfarm", "nfp", "unemployment", "gdp", "ecb", "boe", "boj", "snb", "rba",
    "rbnz", "boc", "powell", "lagarde", "bailey", "ueda", "retail sales", "pmi", "payrolls",
    "central bank", "quantitative", "qe", "taper", "recession", "tariff",
]
MEDIUM = [
    "speech", "minutes", "forecast", "trade balance", "current account", "industrial production",
    "consumer confidence", "housing", "ppi", "wage", "employment", "manufacturing", "services",
    "budget", "election", "sanction", "opec", "oil",
]
TAG_WORDS = {
    "rate": ["rate", "fed", "ecb", "boe", "boj", "rba", "rbnz", "boc", "snb"],
    "inflation": ["cpi", "inflation", "ppi"],
    "jobs": ["non-farm", "nonfarm", "nfp", "payroll", "unemployment", "employment"],
    "growth": ["gdp", "pmi", "retail sales", "industrial production"],
    "politics": ["election", "tariff", "sanction", "budget", "government"],
    "energy": ["oil", "opec", "energy"],
}


def _strip_ns(tag):
    return tag.split("}", 1)[-1].lower()


def _text(el):
    return "".join(el.itertext()).strip() if el is not None else ""


def _parse_date(value):
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return (dt - dt.utcoffset()).replace(tzinfo=None)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(value.replace("Z", "+0000") if fmt.endswith("%z") else value, fmt)
            if dt.tzinfo is not None:
                dt = (dt - dt.utcoffset()).replace(tzinfo=None)
            return dt
        except ValueError:
            continue
    return None


def parse_feed(text, source="feed"):
    """RSS or Atom -> [{title, link, published, summary, source}]."""
    try:
        root = ET.fromstring(text.encode("utf-8") if isinstance(text, str) else text)
    except ET.ParseError:
        return []
    items = []
    for el in root.iter():
        if _strip_ns(el.tag) not in ("item", "entry"):
            continue
        title = link = published = summary = None
        for child in el:
            tag = _strip_ns(child.tag)
            if tag == "title":
                title = _text(child)
            elif tag == "link":
                link = child.get("href") or _text(child)
            elif tag in ("pubdate", "published", "updated", "date"):
                published = published or _parse_date(_text(child))
            elif tag in ("description", "summary", "content"):
                summary = summary or _text(child)
        if title:
            items.append({"title": re.sub(r"<[^>]+>", " ", title).strip(),
                          "link": link, "published": published,
                          "summary": re.sub(r"<[^>]+>", " ", summary or "").strip()[:400],
                          "source": source})
    return items


def tag_item(item):
    blob = (" " + (item.get("title") or "") + " " + (item.get("summary") or "") + " ").lower()
    currencies = []
    for code, words in CURRENCIES.items():
        if any(w in blob for w in words):
            currencies.append(code)
    impact = "low"
    if any(w in blob for w in HIGH):
        impact = "high"
    elif any(w in blob for w in MEDIUM):
        impact = "medium"
    tags = []
    for tag, words in TAG_WORDS.items():
        if any(w in blob for w in words):
            tags.append(tag)
    item["currencies"] = currencies
    item["impact"] = impact
    item["tags"] = tags
    return item


def _fetch(url, timeout=20, user_agent=None):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent or "forex-trader-skill/1.0",
                                               "Accept": "application/rss+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def collect(cfg, feeds=None, offline=None, hours=24, now=None, pause=0.2):
    now = now or timeutil.utc_now()
    items, errors = [], {}
    if offline:
        files = [os.path.join(offline, f) for f in sorted(os.listdir(offline))
                 if f.lower().endswith((".xml", ".rss", ".atom"))]
        for f in files:
            try:
                with open(f, encoding="utf-8") as fh:
                    items.extend(parse_feed(fh.read(), source=os.path.basename(f)))
            except OSError as e:
                errors[f] = str(e)
    else:
        for url in feeds or ((cfg.get("news") or {}).get("feeds") or []):
            try:
                items.extend(parse_feed(_fetch(url, user_agent=(cfg.get("news") or {}).get("user_agent")),
                                        source=url))
            except Exception as e:
                errors[url] = str(e)
            time.sleep(pause)
    cutoff = now - datetime.timedelta(hours=float(hours))
    out, seen = [], set()
    for it in items:
        it = tag_item(it)
        key = re.sub(r"\W+", " ", (it.get("title") or "").lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        if it.get("published") and it["published"] < cutoff:
            continue
        it["published"] = timeutil.iso(it["published"]) if it.get("published") else None
        out.append(it)
    out.sort(key=lambda x: x.get("published") or "", reverse=True)
    return out, errors


def summarize(items):
    by = {}
    for it in items:
        for c in it.get("currencies") or []:
            slot = by.setdefault(c, {"items": 0, "high": 0, "medium": 0, "titles": []})
            slot["items"] += 1
            if it["impact"] in ("high", "medium"):
                slot[it["impact"]] += 1
                if len(slot["titles"]) < 3:
                    slot["titles"].append(it["title"])
    for c, slot in by.items():
        slot["risk"] = "high" if slot["high"] else ("medium" if slot["medium"] else "low")
    return by


def cmd_fetch(args, cfg, now):
    items, errors = collect(cfg, feeds=args.feed, offline=args.offline, hours=args.hours, now=now)
    wanted = [c.upper() for c in args.currencies.split(",")] if args.currencies else None
    if wanted:
        items = [it for it in items if set(it.get("currencies") or []) & set(wanted)]
    summary = summarize(items)
    doc = {"as_of": timeutil.iso(now), "hours": args.hours, "items": items,
           "currencies": summary, "errors": errors}
    if args.write:
        store.save_json(cfg, "news.json", doc)
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
        return 0
    rows = [[it["published"][11:16] if it.get("published") else "", it["impact"].upper(),
             ",".join(it.get("currencies") or []), it["title"][:78]] for it in items[:args.limit]]
    print(cli.table(rows, ["UTC", "IMPACT", "CCY", "HEADLINE"]))
    print("")
    for c, s in sorted(summary.items()):
        print("  %s risk %-6s  %d item(s), %d high" % (c, s["risk"], s["items"], s["high"]))
    if errors:
        print("\nfeeds that did not answer: " + ", ".join(errors))
    if args.write:
        print("\n-> trader/news.json")
    return 0


def cmd_check(args, cfg, now):
    doc = store.load_json(cfg, "news.json")
    if not doc:
        raise SystemExit("no trader/news.json: run news.py fetch --write")
    c = (args.currency or "").upper()
    if not c:
        print(json.dumps(doc.get("currencies") or {}, indent=2))
        return 0
    s = (doc.get("currencies") or {}).get(c)
    if not s:
        print("%s: no news in the last %s hours" % (c, doc.get("hours")))
        return 0
    print("%s risk: %s (%d items, %d high, %d medium)"
          % (c, s["risk"], s["items"], s["high"], s["medium"]))
    for t in s.get("titles") or []:
        print("  - " + t)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="FX news monitor")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="pull the feeds and tag the currencies")
    cli.add_common(f)
    f.add_argument("--hours", type=float, default=24)
    f.add_argument("--currencies", default=None, help="USD,EUR,...")
    f.add_argument("--feed", action="append", default=None)
    f.add_argument("--offline", default=None, help="folder of .xml fixtures")
    f.add_argument("--limit", type=int, default=25)
    f.add_argument("--write", action="store_true")

    c = sub.add_parser("check", help="read trader/news.json")
    cli.add_common(c)
    c.add_argument("--currency", default=None)

    args = p.parse_args(argv)
    cfg = cli.cfg_from(args)
    now = cli.now_from(args, cfg)
    if args.cmd == "fetch":
        return cmd_fetch(args, cfg, now)
    return cmd_check(args, cfg, now)


if __name__ == "__main__":
    sys.exit(main())
