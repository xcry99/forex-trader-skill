---
name: trader-entry
description: >-
  Forex Entry Timer. Watches a session's opening range bar by bar and reports
  the first completed M5 close beyond it, long above the range high and short
  below the range low, or a break of a price level you name. Reads trader/scan.json
  and writes trader/entry.json. Use when the user says "is it triggering",
  "wait for the breakout", "watch EURUSD", "did London break", or between
  /trader-scanner and /trader-exit.
argument-hint: "check SYMBOL [--setup orb|level] [--side auto|buy|sell] | scan | watch SYMBOL"
---

# trader-entry

The Forex Entry Timer is the moment-of-entry rule. It waits for the opening
range to finish, then takes the first **completed** M5 close beyond it. Long
above the range high, short below the range low. Nothing is predicted: the
status is `waiting`, `triggered` or `expired`, and the reason is one line.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/entry.py" check EURUSD --setup orb --session london
python "${CLAUDE_SKILL_DIR}/entry.py" check GBPUSD --side sell --write
python "${CLAUDE_SKILL_DIR}/entry.py" check EURUSD --setup level --level 1.0850
python "${CLAUDE_SKILL_DIR}/entry.py" scan --write
python "${CLAUDE_SKILL_DIR}/entry.py" watch EURUSD --every 30 --until "2026-11-06 11:30"
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`, `--setup orb|level`,
`--side auto|buy|sell`, `--session`, `--or-minutes`, `--confirm close|wick`,
`--level PRICE`, `--write`. `watch` adds `--every SECONDS`, `--until` and
`--max-errors`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/scan.json` | read by `scan`: the hits from `/trader-scanner` |
| `trader/entry.json` | written with `--write`: `entries[]` and `counts` |
| `trader/config.json` | read: `strategy.orb.session`, `or_minutes`, `confirm` |

An entry record has `symbol`, `setup`, `side`, `status`, `price`, `at`, `bar`,
`or_high`, `or_low`, `or_mid`, `level`, `pct_beyond`, `session`, `day` and
`reason`. The plan and the sizing steps consume `status == "triggered"`,
`side` and `price`.

## The rules

1. **Opening range (`orb`)**: the first `or_minutes` of the session, built
   from completed M5 bars. The range is the high and low of those bars.
2. **Confirmation (`--confirm close`, the default)**: the first completed bar
   whose **close** is above the high (buy) or below the low (sell). With
   `wick`, the bar's high or low is enough; it triggers earlier and is noisier.
3. **Side**: `auto` takes whichever side breaks first. `buy` or `sell` waits
   for that side only.
4. **Level (`--setup level`)**: the same close-beyond rule against a price the
   user names, inside the session window.
5. **Expiry**: when the session ends, the setup is `expired`; a later break is
   not the same trade.

The session windows are the scanner's: London 03:00–12:00, overlap
08:00–12:00, New York 08:00–17:00, Asia 19:00–04:00, all New York time. The
default is London, 30 minutes.

## Daily setups

`scan` reads every hit in `trader/scan.json`. An `orb` hit is re-evaluated
with the opening-range rule. A `daily` hit is already in force, so it is
reported as `triggered` at the current ask (buy) or bid (sell), with the daily
setups in `reason`. This is the same convention `/trader-broker auto` uses.

## Steps

1. `/trader-scanner` writes `scan.json`.
2. `entry.py scan --write` checks every hit at once. Or `check SYMBOL` for one.
3. For a live wait, `watch SYMBOL --every 30`. It prints only when the state
   changes and stops on `triggered`, `expired` or too many errors.
4. `/trader-exit` turns the triggered entry into a stop and a target.

## Data and limits

- M5 bars come from the logged-in MetaTrader 5 terminal, corrected from server
  time to UTC. The forming bar is never used; a bar counts at start + 5 min.
- A missing bar means a thin pair or a halt; the range may not be usable and
  the status stays `waiting` with the reason.
- The entry price is the bar close, not the next tick. Slippage between the
  close and the fill is real, which is why the sizer measures risk from the
  live ask or bid when it is worse than the plan.

## Rules

- Say `waiting`, `triggered` or `expired`, with the bar time and the level.
- Never call a trigger a good trade. It is a rule that fired.
- A trigger that is minutes old is stale; re-check before sending an order.

## What it never does

It never places, sizes or checks an order and never decides which pair to
trade. It only reports whether the entry rule fired.
