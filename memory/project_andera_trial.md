---
name: Andera Work Trial
description: 1-day work-trial build for Andera (andera.ai) — a General Browser Agent for audit evidence collection. Judged on accuracy, generality, scalability, consistency, speed. Trial occurs the day after planning.
type: project
---

Build a system-agnostic browser agent that takes a NL task + input file and produces audit-grade evidence (per-sample folders + aggregate CSV). Runs locally on laptop, Docker allowed, ~1 day build.

**Locked decisions (from planning session):**
- Dashboard: FastAPI + HTMX (chose A)
- Observability: Langfuse self-hosted via docker (chose A)
- Tasks: all 5 spec examples hardened (chose all)
- Orchestration: lightweight in-process state machine with per-sample checkpoints (not Temporal — too heavy for 1 day)
- Browser: LocalPlaywrightSession default, BrowserbaseSession as stub swap
- Agent runtime: LangGraph with SqliteSaver checkpointer
- Models: Claude via LiteLLM, role-routed (Opus planner/judge, Sonnet navigator, Haiku extractor). Swap via `profile.yaml`.
- Storage: filesystem for artifacts (per spec), SQLite for metadata + hash-chained audit log

**Why:** User is preparing for trial where they may receive API keys live — switchability via `profile.yaml` is the decisive design move. Every external dep is behind a Protocol.

**How to apply:** When building, enforce hexagonal architecture (ports & adapters). Contracts in `contracts/` are the only cross-module imports. Every agent node is a pure function. Start from `/Users/reetnandy/Desktop/untitled folder/Andera_WorkTrial/PLAN.md` — that is the source of truth. Reference the 5 example tasks in the spec when building the eval harness.

**Git + commit discipline (user-mandated):**
- Local git repo initialized at Phase -1.
- Small, frequent commits. Conventional Commits format. Never `git add -A`; stage explicit files.
- Every commit preceded by a full `/review` of the staged diff — no P1/P2 findings unresolved before committing.
- One phase = at least one commit, usually 2–5. Split mid-phase at natural seams when diff >300 lines.
- Full details in PLAN.md Part 6.5.

**The 5 tasks to deliver:**
1. 1000 users × GitHub × Workday → CSV (mock Workday via FastAPI)
2. Linear tickets → screenshots + CSV (simplest, MVP milestone)
3. 60 GitHub commits → nested PR/CI/Jira screenshots + CSV (hardest nav)
4. LinkedIn enrichment (bot-detection risk — concurrency=1, stealth, Google fallback)
5. Workday form fill + attachment downloads (uses mock Workday)
