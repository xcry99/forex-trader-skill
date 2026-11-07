---
name: trader-scanner
description: >-
  Forex Chart Scanner. Scans the configured currency pairs for setups on
  MetaTrader 5 data: 20-day breakout/breakdown, bull/bear flag, reclaim or
  rejection of the 20-day average, oversold/overbought RSI, and the session
  opening-range breakout or breakdown on M5 bars. Writes trader/universe.json
  and trader/scan.json for trader-entry and trader-broker. Use when the user
  says "scan the market", "find me FX setups", "what is breaking out",
  "opening range", "London breakout", "oversold pairs", or at the start of a
  trading session before /trader-entry.
argument-hint: "[universe | daily | orb | all] [--session london] [--or-minutes 30]"
---

# trader-scanner

The Forex Chart Scanner is the first step of a trade. It reads MetaTrader 5
bars for the pairs in `strategy.watchlist` and lists the ones that match a
rule. That is all it does. A scan is a list of charts that match a rule, not a
list of pairs to buy or sell, and a setup is not a prediction. Most retail
traders lose money; the scanner does not change that. It only saves you from
flipping through charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).
The scripts import the shared `fx/` package from the repository root, so run
them from the repository (or with the repo on `PYTHONPATH`).

```bash
python "${CLAUDE_SKILL_DIR}/scan.py" universe                        # -> trader/universe.json
python "${CLAUDE_SKILL_DIR}/scan.py" universe --all --live-spreads   # every FX symbol the broker offers
python "${CLAUDE_SKILL_DIR}/scan.py" daily                           # -> trader/scan.json
python "${CLAUDE_SKILL_DIR}/scan.py" orb --session london            # -> trader/scan.json
python "${CLAUDE_SKILL_DIR}/scan.py" all --session london            # daily + opening range, merged
```

Flags: `--config PATH` and `--trader-dir PATH` (default `./trader`), `--json`,
`--no-write`, `--now "2026-11-06 03:30"` (New York time, or UTC with `Z`),
`--session asia|london|overlap|newyork`, `--or-minutes N`,
`--confirm close|wick`, `--all` and `--live-spreads` (universe only).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.json` | written by `universe`: symbol, digits, pip size, contract size, lot range, spread, base/quote |
| `trader/scan.json` | written by `daily`, `orb` and `all`: `kind`, `as_of`, `session`, `day`, `hits[]`, `counts`, `errors` |
| `trader/config.json` | read by all commands: `strategy.watchlist`, `strategy.min_daily_bars`, `strategy.orb` |

`hits[]` is what the next skills consume: `symbol`, `side` (`buy` or `sell`),
`setup` (`daily` or `orb`), `setups[]`, `score`, `price`, `atr_pips`,
`spread_pips`, and for an opening range also `or_high`, `or_low`, `or_mid`,
`level` and `at`.

## The universe

`universe` reads the pairs in `strategy.watchlist` and asks MetaTrader for
their contract specification. `--all` lists every symbol where
`currency_base` and `currency_profit` are both three-letter codes and the
contract size is at least 1,000, which is the broker's FX set. It never
guesses a pip: a 5-digit pair uses 10 points per pip, a 3-digit JPY pair uses
10 points per pip, and a 4-digit pair uses one point.

## The daily scan

`daily` reads 220 D1 bars per pair, drops the bar that is still forming, and
needs at least `strategy.min_daily_bars` (30) completed bars. It keeps the
original five stock setups and adds their short-side mirrors:

| long | short | the rule on daily closes |
| --- | --- | --- |
| `breakout_20d` | `breakdown_20d` | the last close is above every close of the 20 sessions before it (or below every one) |
| `bull_flag` | `bear_flag` | up more than 1.5 % over the 10 sessions before the last 5, then the last 5 closes inside a 0.8 % range |
| `reclaim_ma20` | `reject_ma20` | the close before was under its 20-day average and the last close is above it (or the mirror) |
| `oversold` | `overbought` | RSI 14 (Wilder) under 30 (or over 70) |
| `oversold_bounce` | `overbought_bounce` | RSI was under 30 yesterday and today closed up (or over 70 and closed down) |

The flag thresholds are the stock pack's 10 % run and 5 % range scaled to a
major currency pair. A pair can match more than one rule on the same side;
`score` sums the weights (breakout 2.0, flag 1.5, bounce 1.8, reclaim 1.2,
oversold 1.0). Hits are listed by score, then ATR, then symbol.

## The opening-range scan

`orb` uses the first `--or-minutes` (30 by default) of a Forex session and
takes the first **completed** M5 bar that closes beyond that range. A close
above the range high is a `buy` / `orb_breakout`; a close below the range low
is a `sell` / `orb_breakdown`. This is the stock pack's 09:30–09:45 rule moved
to a 24-hour market.

Sessions are written in New York time (the DST rules are the same ones the
original pack used):

| `--session` | New York window | why |
| --- | --- | --- |
| `asia` | 19:00–04:00 | Tokyo |
| `london` | 03:00–12:00 | default; the London open is FX's opening bell |
| `overlap` | 08:00–12:00 | London + New York, the most liquid hours |
| `newyork` | 08:00–17:00 | US session |

Before the range has finished, the scanner reports it as pending. On a
weekend, a holiday, or after the session ends, it scans the most recent
completed session. A bar counts only once it is complete (start + 5 minutes);
the still-forming bar is never used.

## Data and limits

- Everything comes from the MetaTrader 5 terminal that is already logged in.
  Bars are stamped in the broker's server time (usually EET, UTC+2 or UTC+3);
  `fx.timeutil.bar_utc` converts them to true UTC before any session rule runs.
- M5 history is requested as 2,500 bars, enough for about a week of a
  24-hour pair. D1 history is 220 bars, about ten months.
- A pair the broker does not offer is reported in `errors` and skipped; it
  does not stop the scan. A pair with too little history is counted in
  `counts.short`.
- Spreads shown are the broker's current quoted spread at the moment of the
  scan, not a promise of the fill.

## Steps

1. Once, or when the watchlist changes: `scan.py universe`. Check the pip
   size and contract size of anything unfamiliar.
2. Before a session: `scan.py daily`. Tell the user how many pairs were
   fetched, how many had enough bars, and which setups fired on each side.
3. After the opening range: `scan.py orb --session london` (or `all`). Tell
   the user how many ranges broke and when; a later break is closer to the
   price now.
4. Hand the list to `/trader-entry` (the entry timer) and then
   `/trader-exit` (the stop and target). In autonomous mode, hand it to
   `/trader-broker auto`.

## Rules

- Say what was scanned: how many pairs, how many bars came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never promise a profit or print a projected P&L.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which pair to trade,
never calls the data real time (it is the broker's last completed bars and
ticks), and never promises that a setup works.
