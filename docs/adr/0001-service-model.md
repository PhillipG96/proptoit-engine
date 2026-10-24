# ADR-0001: Private Deal Research Service Model

**Date:** 2026-07-15
**Status:** Accepted
**Decision:** PropToIt operates as a private deal research service for investors, not a public-facing website.

## Context

PropToIt collects county records data (assessor, foreclosure, probate, FSBO) and scores properties for off-market opportunity potential. The question was how to deliver this value to end users.

### Alternatives considered

1. **Public website** — investors browse deals themselves. Problems: property photo copyright issues, low differentiation (PropStream, etc.), requires significant traffic to monetize.
2. **Private service** — curated deal packages delivered to paying investors. Solves photo copyright (internal use), higher margin per customer, defensible as a service relationship.

## Decision

Operate as a private deal research service. Investors subscribe, receive curated weekly deal digests with full analysis packages. PropToIt pipeline runs internally; the product is the research, not the software.

## Consequences

- **Legal risk eliminated** — no public hosting of copyrighted content
- **Higher touch** — requires investor onboarding, relationship management
- **Slower scale** — service model caps at manageable volume vs. unlimited software users
- **Defensible** — edge is scoring + matching logic + delivery, not data access