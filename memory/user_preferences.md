---
name: User preferences — engineering style
description: Reet's stated preferences for how code gets built — modularity, reusability, startup efficiency mindset. Industry-standard patterns preferred; pragmatic local implementations acceptable when production swap path is clean.
type: feedback
---

**Rule:** Write modular, reusable code. Hexagonal architecture. Every external dependency behind a Protocol. One config file controls all swaps.

**Why:** User is building for a startup work trial (Andera). Startups value efficiency and pattern-matching to production-grade architecture. In an earlier iteration user explicitly rejected a laptop-scale design as "not scalable, not industry-standard." User wants the *shape* of production systems even when the *implementation* is local.

**How to apply:**
- Always propose ports-and-adapters / hexagonal designs, not monolithic services.
- Always include a `profile.yaml` (or equivalent) that switches implementations without code changes.
- Default to modular monolith (one process, clean seams) over microservices, unless scale genuinely demands distribution.
- Prefer a `Protocol` interface + multiple implementations over inheritance hierarchies.
- Size the solution to the constraint (user clarified "1 day local laptop" after the first overbuilt proposal) — don't re-propose Kubernetes/Temporal/Kafka when a SQLite-backed state machine with the same interface suffices.
- User may be confused by framework jargon — when explaining, include a plain-English "what this actually does" alongside the technical name.
