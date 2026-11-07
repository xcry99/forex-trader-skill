---
name: trader-broker
description: >-
  Autonomous Forex Broker for MetaTrader 5. Runs the full pipeline with no
  per-order confirmation: scan, news, entry, exit, Time Machine, size, risk,
  execute, journal. Also shows the account, positions and orders, closes or
  flattens positions, and syncs closed trades. On a live account it refuses
  without --allow-live. Use when the user says "trade autonomously", "run the
  bot", "execute", "open the trade", "close it", "flatten", "what is open",
  or "start the loop".
argument-hint: "auto [--once|--loop] [--dry-run] | execute | status | positions | close SYMBOL | flatten | sync"
---

# trader-broker

The Forex Broker is the only skill that can send an order to MetaTrader 5. It
runs without a per-order "yes": the user asked for an autonomous experiment.
The risk check is still the gate. An order is sent only when
`/trader-risk` wrote a PASS for that exact fingerprint today, and on a live
account only with `--allow-live`.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/broker.py" account
python "${CLAUDE_SKILL_DIR}/broker.py" positions
python "${CLAUDE_SKILL_DIR}/broker.py" show
python "${CLAUDE_SKILL_DIR}/broker.py" execute --dry-run
python "${CLAUDE_SKILL_DIR}/broker.py" auto --once
python "${CLAUDE_SKILL_DIR}/broker.py" auto --loop --interval 300
python "${CLAUDE_SKILL_DIR}/broker.py" status
python "${CLAUDE_SKILL_DIR}/broker.py" close EURUSD
python "${CLAUDE_SKILL_DIR}/broker.py" flatten
python "${CLAUDE_SKILL_DIR}/broker.py" sync
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`. `auto` adds
`--loop`, `--interval SECONDS`, `--session`, `--or-minutes`,
`--max-candidates N`, `--news`, `--dry-run`, `--allow-live`. `execute` adds
`--dry-run`, `--allow-live`. `close` takes `SYMBOL` or `--all`, `--volume`,
`--reason`. `flatten` takes `--all`. `sync` takes `--days`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/config.json` | read: account, MT5, risk, strategy, journal |
| `trader/scan.json`, `plan.json`, `orders.json` | read by `show` and `execute` |
| `trader/state/risk.json` | read: today's PASS fingerprints |
| `trader/state/broker.json` | written: placed orders, tickets, status |
| `trader/decisions.jsonl` | appended: every cycle, candidate, skip, block, fill and close |
| `trader/journal.jsonl` | appended by `sync`: closed trades |

## The autonomous cycle (`auto`)

1. **Clock and account.** Read equity, free margin and open positions.
2. **Flat-by.** If a position is still open at `risk.flat_by` (16:55 New
   York), close it. This is the original "flat by the close" rule.
3. **Attach brackets.** If a position has no SL/TP, restore them from the
   order record.
4. **Market check.** Outside Sunday 17:00 – Friday 17:00 ET, or on an FX
   holiday, skip the cycle, sync the journal and stop (or sleep in `--loop`).
5. **Scan.** Run the daily scan and the session opening-range scan over
   `strategy.watchlist`, merge hits by symbol and side, and log the top
   candidates. News is read from the cached `trader/news.json`; `--news`
   refreshes it from the RSS feeds first. A high-impact item only blocks an
   entry when `strategy.news.avoid_high_impact_minutes` is above 0.
6. **Entry.** For an ORB hit, re-evaluate the first completed M5 close beyond
   the range. For a daily hit, the entry is the current ask (buy) or bid
   (sell). Non-triggered hits are skipped with the reason.
7. **Plan.** ORB uses the range midpoint as the stop and falls back to
   1.5 × M5 ATR if the midpoint is too close or too far. A daily setup uses
   1.5 × D1 ATR. The target is 1R. The stop is refused if it is inside 0.05 %
   of the price.
8. **Time Machine.** Replay each plan; store target-first, stop-first,
   expectancy and verdict in the plan. It ranks, it does not veto.
9. **Size.** Turn each plan into lots with the max-loss, margin-cap,
   max-order and free-margin limits. A refusal is logged, not forced.
10. **Rank.** Sort by Time Machine expectancy, then by setup score.
11. **Risk.** Check the whole batch at once, save the verdicts, print the
    report, log every BLOCK.
12. **Execute.** Send only the PASS fingerprints, re-checking the live price
    first: if the target is already reached or the stop already hit, skip.
13. **Journal.** Pull closed positions from MT5 into `journal.jsonl`.

`--dry-run` stops before sending and prints how many orders would go.
`--loop --interval 300` repeats the cycle every five minutes.

## The risk gate

The broker does not trust its own memory. `_ready()` reads
`trader/state/risk.json`, checks that it is from today, and looks for the
order's fingerprint. No match, no order. A changed lot size, stop, target or
entry is a different order and needs a fresh PASS.

## Live accounts

`gateway.account().is_demo` decides. A demo account needs nothing. A live
account raises `this is a LIVE account. Re-run with --allow-live if you really
mean it.` This is a deliberate guard, not a risk rule: the pack is built for a
demo experiment.

## Commands

| command | what it does |
| --- | --- |
| `account` | login, server, balance, equity, margin, free margin, leverage, floating P/L |
| `positions` | ticket, symbol, side, lots, open, now, SL, TP, pips, P/L |
| `show` | `orders.json` with READY or BLOCKED and the reason |
| `execute` | send the orders that passed risk; `--dry-run` to preview |
| `auto` | the full cycle; `--once` for one pass, `--loop` to repeat |
| `close SYMBOL` | close that symbol's position (or `--all`, `--volume`) |
| `flatten` | close every position this agent opened (magic number) |
| `sync` | pull closed deals into the journal |
| `status` | account + positions + orders today + last risk check + journal metrics |

## The bracket

Every entry is sent with an attached SL and TP (`TRADE_ACTION_DEAL` with
`sl` and `tp`), so MetaTrader holds the bracket server-side even if the agent
stops running. A market order is retried twice on a requote, price change or
price off. The filling mode is chosen from the symbol's `filling_mode`
(IOC, FOK or RETURN) unless `mt5.filling` overrides it.

## Steps

1. Open the MT5 terminal, log in to the demo account, and enable
   **Tools > Options > Expert Advisors > Allow Algo Trading**.
2. `broker.py account` to confirm the login, balance, leverage and demo mode.
3. `broker.py auto --dry-run --once` to see the full decision path without
   sending anything.
4. `broker.py auto --once` for one real cycle.
5. `broker.py auto --loop --interval 300` for the experiment.
6. `broker.py status` and `/trader-journal report` to review.

## Rules

- Report the account mode (demo/live) and the balance before any execution.
- Say what was sent, at what price, with which SL/TP and ticket.
- A skipped order is a result: say the reason.
- Never claim a fill before MetaTrader returns a ticket.
- A stop is a plan, not a guarantee. Slippage and gaps are real.

## What it never does

It never bypasses the risk check, never trades a symbol outside the FX set,
never opens an order on a live account without `--allow-live`, and never
invents a ticket or a price.
