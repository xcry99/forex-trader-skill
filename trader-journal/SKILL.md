---
name: trader-journal
description: >-
  Forex Trade Journal. Pulls every closed position from MetaTrader 5 deal
  history into trader/journal.jsonl, attaches the agent's plan (setup, risk,
  confidence, Time Machine odds) by position ticket, and reports win rate,
  profit factor, expectancy in R, drawdown, Sharpe per trade, streaks and the
  breakdown by symbol, setup and session. Also lists the agent's decision log
  and exports CSV. Use when the user says "how am I doing", "show my trades",
  "performance", "journal", "win rate", "drawdown", or at the end of a session.
argument-hint: "sync | report [--write] | trades [--limit 20] | decisions [--limit 30] | export [--csv FILE]"
---

# trader-journal

The Forex Trade Journal is built from MetaTrader's own deal history, not from
the agent's memory. If the terminal says the position closed, it is in here.
The plan (setup, risk, confidence, Time Machine odds) is attached by position
ticket, so the performance can be sliced by the rule that produced the trade.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/journal.py" sync
python "${CLAUDE_SKILL_DIR}/journal.py" report
python "${CLAUDE_SKILL_DIR}/journal.py" report --write --json
python "${CLAUDE_SKILL_DIR}/journal.py" trades --limit 20
python "${CLAUDE_SKILL_DIR}/journal.py" decisions --limit 30
python "${CLAUDE_SKILL_DIR}/journal.py" export --csv trader/trades.csv
```

Flags: `--config`, `--trader-dir`, `--json`. `sync` takes `--days N` (default
365). `report` takes `--write`. `trades` and `decisions` take `--limit`.
`export` takes `--csv`.

## What it reads and writes

| file | what |
| --- | --- |
| MT5 deal history | the truth: entry and exit deals per position |
| `trader/state/broker.json` | read: position ticket → plan, max loss, setup, session |
| `trader/journal.jsonl` | appended by `sync`: one record per closed position |
| `trader/decisions.jsonl` | read by `report` and `decisions`: the agent's log |
| `trader/performance.json` | written by `report --write` |
| `trader/trades.csv` | written by `export` |

## The trade record

`sync` groups the deals by `position_id`, averages the entry and exit prices
by volume, and computes:

- `pips`: the price move in pips, signed by side, using the pair's real pip
  size (10 points on a 5-digit pair, 10 points on a 3-digit JPY pair);
- `gross`, `commission`, `swap`, `net`: all in the account currency;
- `risk_usd`: the plan's max loss, when the ticket matches;
- `r_multiple`: `net / risk_usd`, so a target hit is about +1R and a stop
  about −1R (before costs and slippage);
- `setup`, `session`, `confidence`, `timemachine`: the decision context;
- `opened`, `closed`: UTC timestamps.

`sync` is idempotent: a position already in the journal is not appended again.

## The report

```
FOREX TRADER - PERFORMANCE
============================================================
Starting balance : 1000.0
Ending balance   : 1012.4
Return           : 1.24%
Trades           : 8 (5 W / 3 L / 0 BE)
Win rate         : 62.50%
Net              : 12.4
Profit factor    : 1.41
Expectancy       : 1.55 per trade
Avg win / loss   : 4.2 / -3.1
Avg R            : 0.31
Total pips       : 74.0
Max drawdown     : 6.2 (0.62%)
Sharpe/trade     : 0.84
Streaks          : 3 wins / 2 losses
```

Then the breakdown by symbol, setup and session, and the agent's decision
counts: cycles, candidates, executed, blocked, skipped, and the block reasons
by rule (`window=4, loss=2, ...`). That last block is the honest picture of
why the agent did not trade.

## The decision log

`decisions.jsonl` records, in order:

- `cycle`: every autonomous pass, with equity, balance, positions, session;
- `candidate`: every scan hit that entered the pipeline;
- `skipped`: entry not triggered, plan refused, size refused, market closed;
- `blocked`: the risk reasons, with the stage (`risk` or `broker`);
- `executed`: a real fill, with ticket, price, lots, setup and risk;
- `closed`: a close or flatten, with ticket, price and reason.

`decisions --limit 30` prints the newest first. It is the audit trail for the
experiment: you can see what the agent saw, what it chose, and why.

## Steps

1. `journal.py sync` after any close, or let `/trader-broker auto` do it at
   the end of every cycle.
2. `journal.py report` for the numbers.
3. `journal.py decisions` when a result needs an explanation.
4. `journal.py export` to analyse the trades in a spreadsheet.

## Data and limits

- The journal depends on MetaTrader's history. If the terminal has not
  downloaded the deals yet, `sync` may miss them; re-run later.
- A partial close is grouped with its position, so the volume-weighted exit
  price is used.
- The `starting_balance` for the return and drawdown is
  `account.starting_balance` (1,000 in this experiment), not the demo
  account's real 5,000 balance.
- Sharpe per trade is a simple per-trade ratio, not an annualized Sharpe. It
  is a shape indicator, not a fund fact sheet.

## Rules

- Report what the terminal recorded. Never estimate a missing fill.
- Say "closed trades", "win rate", "expectancy in R"; do not extrapolate a
  small sample into a promise.
- Costs matter: commission and swap are included in `net` and in `R`.
- A backtest or a Time Machine result is not a journal trade. Keep them apart.

## What it never does

It never places, modifies or closes an order and never rewrites history. It
only reads the terminal and reports.
