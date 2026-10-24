# ADR-0001: Investor-Weighted Deal Score Formula

## Status: Accepted (2026-05-22)

## Context

Research synthesis of real estate investor deal evaluation frameworks (70% Rule, BRRRR, wholesaling criteria) and analysis of PropToIt's current scoring system identified critical gaps:
- Binary distress scoring treated pre-foreclosure same as FSBO
- No signal type weighting or multi-signal compounding
- Age and condition double-counted
- Missing location quality, rehab cost estimation, days-since-last-sale signals

## Decision

Adopt investor-weighted deal score formula with 7 signal tiers:

| Signal | Weight | Implementation |
|--------|--------|----------------|
| Equity Spread (ARV - purchase cost) | 35% | Normalized by 75th percentile of positive equity |
| Distress Signals (type-weighted, compounded) | 20% | Pre-foreclosure(5) > Probate(4) > Code(3.5) > Tax(3) > Vacancy(2) > FSBO(1) |
| Location Quality | 15% | Zip-level appreciation trends + comps velocity |
| Condition / Rehab Cost | 15% | Street View condition score → rehab cost estimate |
| Absentee Owner | 5% | Owner mailing address analysis (PO Box/out-of-state) |
| Days Since Last Sale | 3% | Derived from lastSaleDate |
| Price/Sqft Gap | 3% | assessedValue÷sqft vs neighborhood median |
| Property Age | 2% | Built before 1980 |
| Lot Size | 2% | Development potential |

## Multi-signal compounding

Properties with 2+ distress signals get a compounding urgency multiplier:
- 2 signals: ×1.1
- 3 signals: ×1.25
- 4+ signals: ×1.5

## Consequences

- **Positive:** Scores now reflect actual investor decision-making. Pre-foreclosure properties jump ahead of FSBOs. Age+condition merged into unified rehab risk.
- **Negative:** Requires zip-level appreciation data (new data source needed). Street View condition scoring pipeline must be built first for equity accuracy.
- **Risk:** Location quality metric depends on historical sales velocity per zip — may take months to accumulate enough data for meaningful trends.
