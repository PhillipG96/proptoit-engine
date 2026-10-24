# ADR-0004: Weekly Digest Delivery Format

**Date:** 2026-07-15
**Status:** Accepted
**Decision:** One-page PDF tear sheets delivered weekly via email, with tap-to-respond feedback.

## Context

Investors need deal information in a format they can consume quickly — on phones, in cars, at meetings. The delivery format affects whether they actually read and act on deals.

### Alternatives considered

1. **Web dashboard** — investors log in to browse deals. Problem: login friction, investors don't want another tab to check.
2. **Email with inline content** — deals in email body. Problem: formatting breaks across clients, can't save/forward easily.
3. **PDF attachment** — one-page tear sheet per deal. Scannable, saveable, forwardable.
4. **SMS/Telegram alerts** — deal summaries as messages. Good for Premium tier, not universal.

## Decision

- **Primary format:** One-page PDF tear sheet per deal
- **Cadence:** Weekly Monday digest with 3-5 deals
- **Feedback:** Tap-to-respond buttons (pursue/pass/save/skip)
- **Premium tier:** Adds Telegram + SMS delivery, real-time MCP alerts

## Consequences

- PDF generation pipeline needed (deal data → formatted PDF)
- Feedback must be low-friction (selection buttons, not typing)
- Skip button is essential — some weeks investors are busy
- "Close calls" shown when zero exact matches, with label