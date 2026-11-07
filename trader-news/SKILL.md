---
name: trader-news
description: >-
  Forex News Monitor. Reads free public FX news RSS feeds (ForexLive, FXStreet,
  DailyFX), tags every headline with the currencies it mentions and an impact
  level from the events that move FX (rate decisions, CPI, NFP, GDP, PMIs,
  central-bank speakers), and writes trader/news.json. Use when the user asks
  "what is moving the dollar", "any news on the euro", "is there event risk
  today", "check the news before I trade", or as context before /trader-entry
  or /trader-broker auto.
argument-hint: "[fetch | check] [--currencies USD,EUR] [--hours 24]"
---

# trader-news

The Forex News Monitor answers one question: what could move this pair right
now? It reads public RSS feeds, tags each item with the currencies it mentions
and an impact level, and stores the result in `trader/news.json`. It is
context, not a signal, and by default it does not block a trade.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/news.py" fetch --hours 24 --write
python "${CLAUDE_SKILL_DIR}/news.py" fetch --currencies USD,EUR --limit 15
python "${CLAUDE_SKILL_DIR}/news.py" fetch --offline tests/fixtures/news --json
python "${CLAUDE_SKILL_DIR}/news.py" check --currency USD
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`, `--hours`,
`--currencies USD,EUR,...`, `--feed URL` (repeatable), `--offline FOLDER`,
`--limit N`, `--write`. `check` reads the last written `news.json` and needs no
network.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/news.json` | `as_of`, `hours`, `items[]`, `currencies{}`, `errors{}` |
| `trader/config.json` | `news.feeds`, `news.user_agent`, `strategy.news.hours`, `strategy.news.currencies` |

Each item has `title`, `link`, `published` (UTC), `summary`, `source`,
`currencies[]`, `impact` (`low`, `medium`, `high`) and `tags[]` (`rate`,
`inflation`, `jobs`, `growth`, `politics`, `energy`).

## How it tags

- **Currencies**: keyword sets per currency. `USD` includes dollar, Fed, FOMC,
  Powell, Treasury, NFP; `EUR` includes euro, ECB, Lagarde, Eurozone; and so on
  for GBP, JPY, CHF, CAD, AUD and NZD.
- **Impact**: `high` for FOMC, rate decisions, CPI, inflation, NFP,
  unemployment, GDP, central banks, retail sales, PMI, payrolls, QE, taper,
  recession and tariffs. `medium` for speeches, minutes, forecasts, trade
  balance, industrial production, confidence, PPI, wages, elections, sanctions
  and oil. Everything else is `low`.
- **Deduplication**: the same headline from two feeds is kept once, and items
  older than `--hours` are dropped.
- **Summary**: per currency, the number of items and the highest impact. A
  currency is `high` risk if any high-impact item mentions it.

## News as a gate (off by default)

`strategy.news.avoid_high_impact_minutes` is `0`, so a high-impact headline is
reported but does not stop an entry. If the user wants a blackout window, set
it to a positive number of minutes; the autonomous broker then skips an entry
whose pair has a high-impact item inside that window. This is a user choice,
not a hidden rule.

## Steps

1. `news.py fetch --hours 24 --write` before a session, or on a schedule.
2. Read the per-currency risk block: which currencies have high-impact news.
3. For a specific pair, check both legs: EURUSD needs EUR and USD, GBPJPY
   needs GBP and JPY. A high-impact item on either leg is event risk.
4. Pass the context to `/trader-entry` or let `/trader-broker auto` include it
   in the decision log.

## Data and limits

- The feeds are free and unofficial. They can lag, change, rate-limit or go
  down; `errors{}` says which feed did not answer. Never present a partial
  fetch as complete.
- Headline tagging is keyword-based. It can miss a subtle story or tag a
  market wrap as high impact. Read the headline before acting on the tag.
- Times are UTC in the JSON and New York in the human summary where relevant.

## Rules

- Report what the feed said, with its timestamp. Do not invent a reason for a
  move.
- Say "high-impact event risk", not "the pair will go up" or "do not trade".
- If the feeds fail, say so plainly and continue with the rest of the flow.

## What it never does

It never places, sizes or checks an order, never predicts the market
reaction, and never claims the news is complete or real time.
