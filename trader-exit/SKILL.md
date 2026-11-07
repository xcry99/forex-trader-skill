---
name: trader-exit
description: >-
  Forex Exit Planner. Sets the stop and the target before the entry: the stop
  can be the opening-range midpoint, its low or high, a price, a pip distance,
  a percentage or an ATR multiple, and the target is an R multiple or a price.
  Mirrors every rule for shorts and writes trader/plan.json. Use when the user
  says "where is my stop", "set a target", "plan the exit", "1R", "2R",
  "ATR stop", or between /trader-entry and /trader-budget.
argument-hint: "plan --from-entry --stop or-mid --target 1R --write | show"
---

# trader-exit

The Forex Exit Planner answers two questions before the entry: where is the
stop, and where is the target? It writes them to `trader/plan.json`, which the
Time Machine, the sizer and the broker all read. A stop is a plan, not a
guarantee: in a fast market it can fill past its price.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/exit.py" plan --from-entry --stop or-mid --target 1R --write
python "${CLAUDE_SKILL_DIR}/exit.py" plan --from-scan --symbol EURUSD --stop atr:1.5 --target 2R --write
python "${CLAUDE_SKILL_DIR}/exit.py" plan --symbol GBPUSD --side sell --stop or-high --target 1.5R --write
python "${CLAUDE_SKILL_DIR}/exit.py" show
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`, `--symbol`,
`--side buy|sell`, `--entry PRICE`, `--stop RULE`, `--target RULE`,
`--or-high`, `--or-low`, `--session`, `--setup`, `--flat-by HH:MM`,
`--from-entry`, `--from-scan`, `--write`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | read by `--from-entry`: the first triggered entry, or the one for `--symbol` |
| `trader/scan.json` | read by `--from-scan`: the first hit, or the one for `--symbol` |
| `trader/plan.json` | written with `--write`: entry, stop, target, pips, R, labels, session, flat-by |
| `trader/config.json` | read: `risk.flat_by`, `strategy.orb.session` |

## Stop rules

| `--stop` | the stop sits at |
| --- | --- |
| `or-mid` | the midpoint of the session opening range |
| `or-low` | the range low (for a long) |
| `or-high` | the range high (for a short) |
| `price:X` | the price X |
| `pips:X` | X pips under the entry for a long, over it for a short |
| `pct:X` | X % of the entry price, in the same direction |
| `atr:K` | K × ATR(14) from the session's M5 bars, in the same direction |

The opening-range rules need `or_high` and `or_low` from the entry or scan
record, or `--or-high` / `--or-low`. The ATR rule uses the session's completed
M5 bars when there are at least three, otherwise the last 40 M5 bars. When the
source setup is `daily`, it uses the last 40 completed **D1** bars instead, so
a daily plan is not stopped out by M5 noise.

## Target rules

| `--target` | the target sits at |
| --- | --- |
| `1R`, `1.5R`, `2R`, ... | that multiple of the stop distance beyond the entry |
| `price:X` | the price X |

## Safety arithmetic

- A long's stop must be below the entry and its target above it; a short's is
  the mirror. A plan that violates that is refused.
- The stop must be at least **0.05 % of the price** away. Closer than that is
  inside the spread's noise, and the planner refuses it. This is the original
  stock pack's minimum-risk floor, kept.
- The target must be on the profitable side of the entry. A target the price
  has already passed is refused.
- `flat_by` defaults to `risk.flat_by` (16:55 New York) and is copied into the
  plan so the broker and the alerts know when to flatten.

## Steps

1. `/trader-entry` writes a triggered entry (or use `--from-scan` for a daily
   setup).
2. Pick a stop that makes sense for the setup: the range midpoint for an ORB,
   an ATR multiple for a daily setup, or a structural price.
3. Pick a target of at least 1R. The risk check enforces
   `risk.min_reward_risk` (1.0 by default).
4. `--write`, then `/trader-timemachine`, `/trader-budget`, `/trader-risk`.

## Example

EURUSD long, entry 1.1020, range 1.1000–1.1040, stop at the midpoint 1.1020?
That is the entry, so the planner refuses it. A range stop only works when the
entry is outside the midpoint: a breakout at 1.1050 with the same range gives
a stop at 1.1020 (30 pips) and a 1R target at 1.1080.

## Rules

- Report the stop and the target in prices and pips, and the reward:risk.
- Say "the plan", never "the safe level" or "it cannot lose more".
- A stop order can fill past its price in a gap or a fast market.

## What it never does

It never places, sizes or checks an order. It only computes the bracket.
