# ADR-0003: Hybrid Matching Engine

**Date:** 2026-07-15
**Status:** Accepted
**Decision:** Use hybrid matching — hard filters on non-negotiable criteria, ranked scoring on preference criteria.

## Context

Investor profiles specify criteria for deal matching. The question is how strict to apply each criterion.

### Alternatives considered

1. **Hard filters only** — deal must meet all criteria. Problem: investors get zero deals in slow weeks, leading to churn.
2. **Scored ranking only** — rank all deals by fit, send top 5. Problem: investors get deals outside their budget or location — useless.
3. **Hybrid** — hard filters on budget and location (non-negotiable), ranked scoring on equity, signals, rehab (preferences).

## Decision

Hard filters on:
- **Budget** — max total investment (purchase + rehab)
- **Location** — target neighborhoods/zip codes

Ranked scoring on:
- Equity spread (higher = better)
- Distress signal alignment with investor preferences
- Rehab cost fit
- Property type match

If zero matches pass hard filters, auto-broaden location by 5-mile radius and flag deals as "close calls."

## Consequences

- Investors always see relevant deals (budget/location constraints honored)
- Quality varies by week — transparent "close calls" better than silence
- Requires investor profile to distinguish hard vs. soft criteria