---
name: trader-alerts
description: >-
  Forex Price Alerts. Arms price, stop, target, time, pip-move and spread
  alerts on MetaTrader 5 quotes; `--from-plan` adds the plan's stop, target and
  flat-by time in one command. `check --execute` can close the position when an
  alert fires. Use when the user says "alert me at", "watch this level",
  "tell me when it hits the stop", "flatten at the close", or while a position
  is open.
argument-hint: "add --from-plan | add --symbol EURUSD --type price --price 1.1000 --direction below | check [--execute] | list | quote SYMBOL..."
---

# trader-alerts

The Forex Price Alerts are a second pair of eyes. MetaTrader already holds the
SL/TP bracket server-side, so these alerts are for the things the bracket does
not cover: a level the user names, a pip move, a spread blowout, and the
session's flat-by time. They fire once and stay fired until reset.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/alerts.py" add --from-plan
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol EURUSD --type price --price 1.1000 --direction below
python "${CLAUDE_SKILL_DIR}/alerts.py" add --symbol GBPUSD --type time --at 16:55
python "${CLAUDE_SKILL_DIR}/alerts.py" check
python "${CLAUDE_SKILL_DIR}/alerts.py" check --execute
python "${CLAUDE_SKILL_DIR}/alerts.py" list
python "${CLAUDE_SKILL_DIR}/alerts.py" reset --id EURUSD-2026-11-06-stop
python "${CLAUDE_SKILL_DIR}/alerts.py" quote EURUSD USDJPY
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`. `add` takes
`--from-plan` or `--symbol` + `--type`, plus `--price`, `--direction above|below`,
`--side buy|sell`, `--at HH:MM`, `--pips`, `--ref-price`, `--spread`, `--note`,
`--date`, `--id`. `check` takes `--execute`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/alerts.json` | the armed rules |
| `trader/state/alerts.json` | `fired[]` and `checked_at` |
| `trader/plan.json` | read by `add --from-plan` |

## Alert types

| `--type` | fires when | needs |
| --- | --- | --- |
| `price` | bid crosses the level in `--direction` | `--price`, `--direction` |
| `stop` | bid (long) or ask (short) reaches the stop | `--price`, `--side` |
| `target` | bid (long) or ask (short) reaches the target | `--price`, `--side` |
| `time` | New York wall clock reaches `--at` | `--at HH:MM` |
| `pips_move` | the mid moved at least N pips from a reference | `--pips`, optional `--ref-price` |
| `spread` | the quoted spread is at least N pips | `--spread` |

`--from-plan` arms three rules at once from `plan.json`: the stop, the target
and a `time` rule at the plan's `flat_by` (16:55 New York by default). Their
ids are `SYMBOL-DATE-stop`, `SYMBOL-DATE-target` and `SYMBOL-DATE-flat`.

## Firing and resetting

- A rule is active only on its `date` (New York date). An old rule does not
  fire on a new day.
- `check` prints every rule that hit, adds its id to `fired[]`, and does not
  print it again.
- `reset` re-arms everything, or one id with `--id`.
- `check --execute` also closes every position on the rule's symbol when the
  rule fires. Use it deliberately: a `time` rule with `--execute` is the
  "flatten by the close" behaviour.

## Steps

1. After `/trader-exit` writes the plan: `alerts.py add --from-plan`.
2. While the position is open, run `alerts.py check` every minute, or in a
   loop from the shell.
3. If the user wants automatic flattening at the close, use
   `alerts.py check --execute` and arm the `time` rule. The autonomous broker
   already flattens at `risk.flat_by`, so do not run both against the same
   position.
4. `alerts.py quote EURUSD` for a live bid/ask/spread snapshot.

## Data and limits

- Quotes come from the logged-in MetaTrader 5 terminal. A closed market has a
  stale tick; the alert may fire on the last price of the week. Check the
  timestamp.
- The stop and target alerts use the bid for a long and the ask for a short,
  the side that would actually close the position.
- Alerts are local to this machine. They do not survive a reboot as a running
  process; the rules do survive in `alerts.json`.

## Rules

- Say which rule fired, with the price and the time.
- Do not describe an alert as a recommendation.
- If `--execute` is on, say what was closed and the ticket.

## What it never does

It never opens a position and never changes a stop or a target. It only rings
(and, with `--execute`, closes).
