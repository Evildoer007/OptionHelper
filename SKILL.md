---
name: option-helper
description: Recommend and explain option payoff structures from a market view. Trigger when the user asks what option structure fits a given underlying, direction, volatility view, price range, path condition, barrier or touch condition, or asks for the payoff, break-even, maximum profit/loss, construction, use case, or comparison of vanilla options, vertical spreads, straddles, strangles, butterflies, condors, barrier options, binary options, digital options, one-touch, or no-touch structures. Do not use for structured products such as airbags, snowballs, accumulators, or full product term-sheet design.
---

# Option Helper

Turn a market view into a concrete option structure, or explain the payoff mechanics of a named structure.

## Reference Loading

Read `references/option-structures.md` before answering. It is the source of truth for notation, payoff conventions, formulas, break-even points, maximum profit/loss, examples, and structure-specific use cases.

Use targeted lookup when possible:

- Directional options: section 2
- Vertical spreads: section 3
- Volatility and range structures: section 4
- Barrier options: section 5
- Binary and digital options: section 6

## Workflow

1. Parse the user view into four dimensions: direction, volatility, path, and magnitude.
2. Select the structure family:
   - Strong directional view: use calls or puts.
   - Moderate directional view with capped payoff acceptable: use vertical spreads.
   - Direction uncertain but volatility view clear: use straddles, strangles, butterflies, or condors.
   - Touch, knock-in, knock-out, or path-dependent condition: use barrier or touch structures.
   - Fixed payout or yes/no outcome: use binary, digital, one-touch, or no-touch structures.
3. Select the exact structure and cite its construction from the reference.
4. State payoff formula, break-even, maximum profit/loss, and key risk.
5. If the user provides a specific underlying and price view, translate the view into strike, barrier, and premium assumptions only as an illustrative example unless market quotes are available.

## Structure Selection Guardrails

Use the user's described construction and path condition to disambiguate exact structures before naming the structure.

Vertical spread mapping:

- `买入较低行权价看涨期权、卖出较高行权价看涨期权` is 牛市看涨价差.
- `卖出较高行权价看跌期权、买入较低行权价看跌期权` is 牛市看跌价差.
- `买入较高行权价看跌期权、卖出较低行权价看跌期权` is 熊市看跌价差.
- `卖出较低行权价看涨期权、买入较高行权价看涨期权` is 熊市看涨价差.
- If the user says they want to sell a put for premium and buy a lower-strike put for protection, recommend 牛市看跌价差, not 熊市看跌价差.

Barrier direction mapping:

- `向上` means the barrier is above the current price and is triggered by touching or breaking the upper barrier.
- `向下` means the barrier is below the current price and is triggered by touching or breaking the lower barrier.
- `敲入` means the option becomes effective only after the barrier is touched.
- `敲出` means the option becomes invalid after the barrier is touched.
- If a call activates only after breaking an upper key level, recommend 向上敲入看涨.
- If a call activates only after first falling to a lower support, recommend 向下敲入看涨.
- If a put activates only after first rallying to an upper resistance, recommend 向上敲入看跌.
- If a put activates only after breaking a lower key level, recommend 向下敲入看跌.

Covered call naming:

- If the user already holds the underlying and wants to sell an upside call for premium, treat `备兑开仓` as `备兑看涨`, and include the canonical name `卖出看涨期权` or `备兑看涨` in the recommendation.

## Answer Format

For recommendations, answer in this order:

1. 推荐结构
2. 适用观点
3. 构造
4. 到期损益
5. 盈亏平衡与最大盈亏
6. 主要风险
7. 替代结构

For a named-structure explanation, answer in this order:

1. 结构定义
2. 构造
3. 损益公式
4. 盈亏平衡与最大盈亏
5. 适用场景
6. 易错点

## Writing Rules

- Use concise professional quantitative-finance language.
- Use `权利金`; do not replace it with `保证金`.
- Do not use `腿`; write the actual option position, such as `买入低行权价看涨期权、卖出高行权价看涨期权`.
- Do not add Greeks unless the user explicitly asks.
- Distinguish expiry payoff from path state. For barrier, touch, and no-touch structures, do not infer payoff from only the terminal price.
- If a requested structure is outside the reference scope, say so directly and do not invent unsupported terms.
