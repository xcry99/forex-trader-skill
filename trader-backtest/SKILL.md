---
name: trader-backtest
description: >-
  Forex walk-forward backtest on MetaTrader 5 history. Replays the live rules
  day by day: the five daily setups and their short mirrors from completed D1
  bars, the session opening-range breakout, the same stop/target arithmetic,
  the same lot sizing, the entry window and the daily trade/loss caps. Reports
  win rate, net, profit factor, expectancy in R, drawdown, equity curve and
  breakdowns by symbol and setup. Use when the user says "backtest", "how
  would this have done", "replay the strategy", or wants evidence before
  letting the agent trade. This is history, not a promise.
argument-hint: "run [--days 90] [--symbols EURUSD,GBPUSD] [--session london] [--write]"
---

# trader-backtest

The Time Machine (`trader-timemachine`) replays **one planned trade** in a
pair's recent sessions. This skill replays **the whole strategy** over a
period: every weekday, every watched pair, both setups, with the live sizing
and the live risk caps.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/backtest.py" run --days 90
python "${CLAUDE_SKILL_DIR}/backtest.py" run --days 180 --write
python "${CLAUDE_SKILL_DIR}/backtest.py" run --days 90 --symbols EURUSD,GBPUSD,USDJPY --json
python "${CLAUDE_SKILL_DIR}/backtest.py" run --days 90 --no-daily      # ORB only
python "${CLAUDE_SKILL_DIR}/backtest.py" run --days 90 --no-orb        # daily only
python "${CLAUDE_SKILL_DIR}/backtest.py" report
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`, `--days N`,
`--symbols A,B`, `--session`, `--or-minutes`, `--confirm close|wick`,
`--spread-pips`, `--slippage-pips`, `--budget`, `--risk-usd`, `--daily-loss`,
`--max-trades`, `--no-daily`, `--no-orb`, `--trades N`, `--write`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | watchlist, session, ORB length, risk caps, budget |
| MT5 terminal | M5 and D1 history; nothing is sent to the broker |
| `trader/backtest.json` | written with `--write`: params, summary, trades, skips, errors |
| `trader/backtest_trades.csv` | written with `--write`: one row per trade |

## The rules it replays

- **Daily setups** — the original five plus their short mirrors
  (`breakout_20d`/`breakdown_20d`, `bull_flag`/`bear_flag`,
  `reclaim_ma20`/`reject_ma20`, `oversold`/`overbought`,
  `oversold_bounce`/`overbought_bounce`) from D1 closes completed *before* the
  entry. A setup is acted on at `risk.no_entries_before` (02:30 New York), the
  first moment the live window allows an entry. Stop 1.5 × ATR(14) on D1,
  target 1R.
- **Opening range** — the first `or_minutes` of the configured session
  (London 03:00–03:30 New York by default), then the first completed M5 close
  beyond the range. Stop at the range midpoint (`or_mid`), ATR fallback if the
  midpoint is inside the minimum-risk floor; target 1R.
- **Merge** — if the daily setup and the ORB agree on a pair and side, one
  trade is taken with the daily plan, exactly like `trader-broker auto`.
- **Sizing** — `trader-budget`'s own function: lots from the max loss per
  trade, the margin cap, the cash buffer and the broker's contract maths.
- **Risk caps** — entry window, `max_trades_per_day`, `max_daily_loss_usd`
  (sum of the max loss at risk, as in live), duplicate pair+side per day,
  `allow_short`, `min_reward_risk`.
- **Exits** — server-side bracket replayed bar by bar: stop, target or
  `flat_by` (16:55 New York). If a bar touches both levels, the stop wins.

## What it does not replay

- **News.** Headlines are not archived with reliable publication times, so the
  news filter cannot be applied to the past. It is context-only by default
  anyway (`avoid_high_impact_minutes: 0`).
- **The Time Machine.** It is advisory and would multiply the runtime; the
  backtest asks what the rules did, not what the simulator would have guessed.
- **Slippage and commissions.** Off by default; `--slippage-pips` adds an
  adverse fill to entries and stop exits. Axi's spread account is
  spread-only, so commission 0 is realistic.
- **Requotes, partial fills, broker rejections.** The backtest assumes every
  sized order fills.

## Reading the output

```
Period        2026-08-09 -> 2026-11-07 (90 calendar days, 64 trading days)
Risk          $10.00/trade, $30.00/day, 3 trades/day, budget $1,000.00
Trades        18 (7 W / 11 L / 0 BE)
Win rate      38.89%
Net           -$12.40
Profit factor 0.86
Expectancy    -$0.69 per trade
Avg R         -0.07
Max drawdown  $48.10 (4.81%)
Trades/day    0.28 over 64 trading days
Skipped       143 (size=118, window=20, trades/day=5)
```

- **Net** is after the spread on every fill, priced by the broker.
- **Avg R** is the average of `net / max_loss`, the same R the live journal
  uses.
- **Max drawdown** is on closed balance, trade by trade.
- **Skipped** matters: with $10 of risk and a 0.01 minimum lot, many pairs are
  too wide to size. A high `size` count is the risk rule working, not a bug.
- The CSV and the `trades` array have entry, exit, stop, target, pips, net, R,
  exit reason, hold time and spread per trade.

## Rules

- Report the period, the symbols and the risk size with the result. A number
  without them is not a backtest.
- Say "the rules did", never "the strategy will".
- Do not tune the config to make one period look good and then present it as
  evidence. If the user wants optimisation, split the period and say so.
- A small sample is a small sample: with these caps a 90-day run is often
  twenty trades. Say how many there were.

## What it never does

It never places, modifies or closes an order, and never writes to the journal,
`orders.json` or the broker state. It only reads history and writes its own
`backtest.json` / CSV.
