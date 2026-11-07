---
name: trader-risk
description: >-
  Forex Risk Manager. Checks every proposed order against the account and the
  risk rules in lots, pips, margin and dollars: max loss per trade, daily loss,
  trades per day, orders per day, reward:risk, required stop, entry window,
  spread, margin cap, free-margin buffer, duplicates, market hours and the
  terminal's algo-trading switch. Writes PASS or BLOCK with the reason to
  trader/state/risk.json. Use when the user says "check the risk", "is this
  allowed", "risk manager", or between /trader-budget and /trader-broker.
argument-hint: "[--orders FILE] [--no-write] [--json]"
---

# trader-risk

The Forex Risk Manager is the hard gate. It reads the orders the sizer wrote
and returns PASS or BLOCK for each one, with every reason spelled out. The
broker only sends an order that passed **this exact check, today**: the state
file stores a fingerprint of the order (symbol, side, volume, type, entry,
stop, target, comment), so changing any of them invalidates the pass.

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/check.py"
python "${CLAUDE_SKILL_DIR}/check.py" --orders trader/orders.json --json
python "${CLAUDE_SKILL_DIR}/check.py" --no-write
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`, `--orders FILE`,
`--no-write`. Exit code is 0 when every order passes, 1 when anything is
blocked.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/orders.json` | read by default: the bracket orders from `/trader-budget` |
| `trader/state/broker.json` | read: orders already placed today, for duplicates and daily limits |
| `trader/state/risk.json` | written: `today`, `pass`, `block`, `results[]` with fingerprints |
| `trader/config.json` | read: `risk.*`, `account.*` |

## The rules

The original stock-pack rules, translated to FX units:

| rule | default | what it checks |
| --- | --- | --- |
| `max_loss_per_trade_usd` | 10 | lots × money risk per lot at the stop |
| `max_daily_loss_usd` | 30 | money already at risk today + this order + earlier passes in the batch |
| `max_trades_per_day` | 3 | entry orders placed today + passes in this batch |
| `max_orders_per_day` | 10 | all orders placed today + passes in this batch |
| `min_reward_risk` | 1.0 | (target − entry) / (entry − stop) |
| `require_stop` | true | every entry needs an SL |
| `allow_short` | true | sells are allowed |
| `allow_margin` | true | margin is allowed |
| `max_margin_pct` | 75 | used margin + new margin, as % of equity |
| `cash_buffer_pct` | 10 | free margin left after the order |
| `max_order_lots` | 10 | order size |
| `min_order_lots` | 0.01 | order size |
| `blocked` | `[]` | symbols never traded |
| `no_entries_before` | 02:30 NY | first entry |
| `no_entries_after` | 11:30 NY | last entry |
| `max_spread_pips` | null | optional spread ceiling |

It also checks, for entries only:

- the symbol is a real FX pair the broker offers and is fully tradable;
- the side is `buy` or `sell`; the volume is a positive multiple of the
  broker's lot step, inside its min and max;
- a limit order has a price;
- the stop is on the losing side of the reference price and the target on the
  winning side;
- the last tick is fresh (under 15 minutes old);
- the market is open (Sunday 17:00 ET to Friday 17:00 ET, plus the FX holiday
  list);
- the terminal's **Allow Algo Trading** switch is on and the broker has not
  disabled the trading API;
- the pair/side was not already placed today and is not duplicated in the
  batch.

## Exits are never blocked

A sell that reduces an existing long, a buy that reduces an existing short, or
an order with `close_ticket`, is an **exit**. Entry rules become warnings on
an exit: getting out must always work. This is the original pack's rule and it
is deliberate.

## The reference price

A market buy is measured from the higher of the plan entry and the live ask; a
market sell from the lower of the plan entry and the live bid. That is the
conservative side of a possible fill, so the risk number is not flattered.

## Reading the output

```
1. PASS BUY EURUSD 0.05 lots
   BUY entry 1.10200, stop 1.10000 (20.0 pips), target 1.10400: risk $10.00
   margin $5.51, reward:risk 1.00, today 0/3 trades, $0.00/30.00 daily loss

2 PASS, 0 BLOCK
```

A block looks like:

```
2. BLOCK SELL GBPUSD 0.12 lots
   BLOCK  loss: 0.12 lots x $12.50 risk per lot = $15.00 at the stop, over your $10.00 max loss per trade
   BLOCK  window: it is 13:00 New York, after your 11:30 last entry
```

## The algo-trading switch

MetaTrader 5 has a global switch: **Tools > Options > Expert Advisors >
Allow Algo Trading**. When it is off, `terminal_info().trade_allowed` is false
and every entry is blocked with that exact instruction. The switch is a
terminal setting, not something this pack can flip. Exits are still allowed,
so a position can always be closed.

## Steps

1. `/trader-budget` writes the order.
2. `check.py` writes the verdict and the fingerprints.
3. `/trader-broker execute` or `auto` sends only the PASS orders.
4. Re-run after changing any order field; the old fingerprint no longer
   matches.

## Rules

- Report every BLOCK reason, not just the first.
- A PASS is a rules check, not a recommendation.
- Never widen a limit silently to make an order pass. If the user wants a
  different limit, they change `trader/config.json` and say so.

## What it never does

It never places, modifies or closes an order. It only reads and judges.
