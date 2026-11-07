---
name: trader-budget
description: >-
  Forex Budget Manager. Turns a plan into lots: how many lots put exactly the
  max loss per trade at risk at the stop, inside the margin cap, the max order
  and the free-margin buffer. Uses the broker's own order_calc_profit and
  order_calc_margin, so JPY crosses and cross pairs come out right. Writes
  trader/orders.json. Use when the user says "how big", "position size",
  "how many lots", "risk 1%", "size it", or between /trader-exit and
  /trader-risk.
argument-hint: "size [--plan FILE] [--budget 1000] [--order-type market|limit] [--write] | limits"
---

# trader-budget

The Forex Budget Manager answers one question: how many lots? The original
stock pack divided dollars by (entry − stop) to get whole shares. FX adds a
contract size, a pip and leverage, so the same question becomes: how many lots
put exactly the max-loss budget at risk at the stop, without breaking the
margin rules?

`${CLAUDE_SKILL_DIR}` is this skill's folder.

```bash
python "${CLAUDE_SKILL_DIR}/size.py" size --write
python "${CLAUDE_SKILL_DIR}/size.py" size --budget 1000 --plan trader/plan.json --write
python "${CLAUDE_SKILL_DIR}/size.py" size --order-type limit --write
python "${CLAUDE_SKILL_DIR}/size.py" limits
```

Flags: `--config`, `--trader-dir`, `--json`, `--now`, `--plan FILE`,
`--budget USD`, `--order-type market|limit`, `--write`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | read by default: symbol, side, entry, stop, target, setup, session |
| `trader/orders.json` | written with `--write`: one bracket order, or `[]` when the plan is refused |
| `trader/state/sizing.json` | written with `--write`: the full sizing arithmetic and the limits |
| `trader/config.json` | read: `account.budget`, `account.leverage`, `risk.*` |

## The arithmetic

```
risk_per_lot = order_calc_profit(side, symbol, 1 lot, entry, stop)   # account currency
risk_pips    = |entry - stop| / pip_size
volume_raw   = min(
    max_loss_per_trade_usd / risk_per_lot,
    (budget * max_margin_pct / 100) / margin_per_lot,
    max_order_lots,
    available_margin / margin_per_lot
)
volume = floor(volume_raw / volume_step) * volume_step
```

`available_margin` is the smaller of the budget's free part and the account's
free margin, after the cash buffer:

```
cash_buffer      = budget * cash_buffer_pct / 100
budget_available = budget - cash_buffer
account_available= margin_free - cash_buffer
available        = min(budget_available, account_available)
```

With the default 1,000 USD budget, 10 USD max loss, 75 % margin cap, 10 %
buffer and 1:1000 leverage, a 20-pip EURUSD stop at 1.1020 gives
**0.05 lots**: 0.05 × 20 pips × 10 USD/pip/lot = 10 USD at risk, about 5.51
USD of margin. The smallest limit binds and is reported as `bound by`.

The reference price is conservative: a market buy is measured from the higher
of the plan entry and the live ask; a market sell from the lower of the plan
entry and the live bid. A limit order uses its own price.

## Refusals

No order is written when:

- one minimum lot (0.01) would risk more than the max loss per trade — the
  message says how much it would risk and suggests a closer stop;
- the reward:risk is under `risk.min_reward_risk`;
- the volume is under `risk.min_order_lots`;
- the plan's stop or target is on the wrong side of the entry;
- the account or budget cannot cover the margin.

A refusal is a valid result, not an error. Report the reason and move on.

## `limits`

`size.py limits` prints the account and risk settings in one screen: budget,
leverage, mode, max loss per trade, daily loss, trades per day, orders per day,
minimum reward:risk, margin cap, max order, cash buffer, entry window, flat-by
time, shorts and margin.

## Steps

1. `/trader-exit` writes the plan.
2. `size.py size --write` produces the bracket order.
3. `/trader-risk` checks it against every limit.
4. `/trader-broker execute` (or `auto`) sends only a PASS.

## Rules

- Report lots, pips at risk, money at risk and margin. Never report a
  projected profit.
- Say "the max loss at the stop", not "the most you can lose": slippage and
  gaps can make the realized loss larger.
- The smallest limit is the honest answer. Do not round up to a bigger lot.

## What it never does

It never places or checks an order and never chooses a pair. It only sizes the
plan it is given.
