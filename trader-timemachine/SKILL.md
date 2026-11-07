---
name: trader-timemachine
description: >-
  Forex Time Machine. Replays a planned trade before any money is risked:
  10,000 Monte Carlo paths drawn from the pair's own M5 returns at the same
  time of day over the last 60 sessions, plus the exact rule re-run on every
  past session where it fired. Reports target-first, stop-first, still-open,
  expectancy in R and a verdict. Use when the user says "what are the odds",
  "replay this trade", "backtest the setup", "is this edge real", or before
  /trader-budget and /trader-risk.
argument-hint: "run [--plan FILE] [--replays 10000] [--seed 19] [--days 60] [--write]"
---

# trader-timemachine

The Forex Time Machine is the reality check between the plan and the order.
It asks the same two questions the stock pack asked:

1. **Monte Carlo** — draw the rest of the holding window 10,000 times from this
   pair's own M5 returns at the same time of day over the last 60 sessions.
2. **History replay** — run the exact rule on every past session where it
   fired and count target first, stop first, or still open at the end.

The holding window runs from the session's open to the plan's `flat_by`
(16:55 New York by default), not just to the London close: a Forex trade can
outlive the session that produced it. It is a simulation, not a prediction.
The future can do things those sessions never did: a central-bank surprise, a
gap, a headline.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --write
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --replays 20000 --seed 7 --write
python "${CLAUDE_SKILL_DIR}/timemachine.py" run --plan trader/plan.json --days 90
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`, `--plan FILE`,
`--replays N`, `--seed N`, `--window N`, `--days N`, `--write`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | read by default: symbol, side, entry, stop, target, session, entry_time |
| `trader/timemachine.json` | written with `--write`: Monte Carlo, history, verdict, notes, seed |
| `trader/config.json` | read: `strategy.timemachine.replays`, `seed`, `window`, `days` |

## How the simulation works

- It loads roughly 60 sessions of M5 bars (about 17,800 bars) from
  MetaTrader, converts server time to UTC, and cuts each session into its
  completed M5 bars. The replay window runs to the plan's `flat_by` (16:55 New
  York), so a London entry is not cut off at the London close.
- The entry bar is the first session bar at or after the plan's `entry_time`.
  The bars after it are the replay window.
- For each replay, the price walks forward one bar at a time. At slot `j` it
  draws a return from the same slot in past sessions, within a `window` of
  ±2 slots. The move is added for a long and subtracted for a short.
- The path stops at the first touch of the stop or the target. A path that
  touches neither by the session's end is "open at end" (0R).
- The seed makes it repeatable: the same plan, bars and seed give the same
  numbers.
- History replay is conservative: if a bar's range touches both the stop and
  the target, the stop wins the tie.

## Reading the output

```
Monte Carlo  10000 replays, seed 19, 48 bars left
  target first    29.4%
  stop first      25.5%
  open at end     45.1%
  expectancy     +0.04 R
History      59 london sessions
  fired           33
  target first    13
  stop first      15
  open at end      5
Verdict: COIN FLIP
```

- **Target first** and **stop first** are the two absorbing outcomes.
- **Open at end** means the plan's `flat_by` time closed it, not the bracket.
- **Expectancy** is the average R per replay: +1 for a target, −1 for a stop,
  0 for open. A verdict above +0.10 R is a `positive edge`, below −0.10 R a
  `negative edge`, otherwise a `coin flip`.
- **History** is the same rule on real past sessions, not a simulation.

## How the autonomous broker uses it

`/trader-broker auto` runs the Time Machine on every candidate plan and ranks
the plans by its expectancy, then by the setup score. It **records** the odds
in the order and the journal, and it **does not block** a trade for a negative
verdict by default. The user asked for an experiment, not for a hidden veto;
the risk check remains the only hard gate. If the user wants a minimum
expectancy, it belongs in the config, not in this skill.

## Data and limits

- It needs at least five usable sessions of M5 history, and the entry must
  leave at least one bar to replay. Otherwise it says why and stops.
- The Monte Carlo is built from recent behaviour, so it is blind to a regime
  change, a scheduled event and a weekend gap.
- 10,000 replays of a full session take a few seconds; `--replays` and
  `--days` trade accuracy for speed.
- It never reads the future and never sees the broker's order book.

## Rules

- Report the seed and the number of replays with the odds, so the result can
  be repeated.
- Say "the simulation says", never "the trade will".
- A positive expectancy is not a promise; a negative one is not a veto unless
  the user made it one.

## What it never does

It never places, sizes or checks an order and never changes the plan. It only
reports what the pair's own recent sessions did.
