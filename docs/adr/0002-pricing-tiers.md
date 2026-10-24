# ADR-0002: Three-Tier Pricing Structure

**Date:** 2026-07-15
**Status:** Accepted
**Decision:** Three pricing tiers ($250/$350/$500) differentiated by deal volume, county access, delivery channels, and execution support.

## Context

Investors have different needs and budgets. A single price either leaves money on the table (power users) or prices out casual users. Tiering lets investors self-select.

### Tier structure

| Feature | Basic $250 | Pro $350 | Premium $500 |
|---------|-----------|----------|------------|
| Deals/week | 3 | 5 | 5+ (no cap) |
| Deal packages | One-page summary | One-page + comp table | Full + outreach templates |
| Counties | Hamilton only | Hamilton + 1 more | All available counties |
| Delivery | Email PDF | Email + Telegram | Email + Telegram + SMS |
| Owner outreach | — | — | Drafted templates |
| MCP connection | — | — | Real-time API access |
| Profile coaching | — | Auto-refinement | Manual + auto |

## Decision

Three tiers with clear differentiation. $250 = deal alerts only. $350 = deeper analysis + multi-county. $500 = full execution support + API access.

## Consequences

- **Upsell path** — investors can start at $250 and move up as they see value
- **MCP as premium feature** — API access justifies the $500 tier for tech-savvy investors
- **Cost scaling** — Premium tier requires more labor (outreach templates, coaching); must keep Premium count manageable