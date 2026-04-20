# Agent System Design — Problems Against the LangGraph Flow

*This doc is about HOW the three problems from `todo.txt` map onto our
actual agent (`AGENT_ARCHITECTURE.md`). It reuses the agent's existing
nodes — `classify → plan → act → observe → verify → extract → judge` —
and shows what we add, where, and why. No code.*

*Scope: planner on known + unknown platforms, auth (simplified to "human
once, saved, tied to IP"), and speed (across samples, within a sample,
across pods, with domain + user rate limiting).*

---

## 0. The agent, recapped

One sample flows through a LangGraph state machine:

```
 classify → plan → [act → observe → verify]* → extract → judge → END
                          ↑         │
                          └─ loop ──┘   (retry ≤3, replan on 2 fails)
```

Two facts that drive the rest of this doc:

- **Within a sample, steps are sequential.** The state machine enforces
  it. `act` must finish before `observe` runs. `observe` before `verify`.
  A 6-step GitHub flow (`goto repo → click PRs → filter → click PR →
  scroll to commits → extract comment`) is 6 iterations of the
  `act-observe-verify` cycle. They cannot be parallelized — step N+1
  depends on the DOM state that step N produced.

- **Across samples, everything is independent.** Sample A does not
  depend on Sample B. Parallelism lives here. Every speed gain we can
  make comes from running more samples at once, NOT from running one
  sample faster (except for per-step latency).

These two facts determine where every speed lever works and where it
doesn't.

---

# Problem 1 — Planner for both known and unknown sites

## The issue

The `plan` node asks Opus for an ordered list of `PlanStep`. For known
platforms (GitHub, Linear, Jira, Workday) this works on the first try
because the model has strong priors. For unknown platforms (custom
in-house ERPs, regional HRIS, vendor portals), the planner hallucinates.
It invents menu names. It proposes clicks on elements that don't exist.
The whole sample fails.

`classify` already picks a `task_type` (extract / form_fill / list_iter
/ navigate / generic). That tells us what SHAPE of task it is, not
whether the PLATFORM is known. We need a second dimension.

## Solution — add one new node before `plan`, and three fallback tiers

### 1.1 New node: `recognize`

Goes after `classify`, before `plan`. Cheap. Multi-signal.

**Inputs:** `start_url`, first page snapshot (requires a preflight goto
at agent start — not counted against plan steps).

**What it checks:**
- Domain against a known-platform list.
- DOM fingerprint (hash of stable structural markers) against cached
  fingerprints.
- A canonical action test (e.g., "find the profile menu" for
  GitHub-family) — does the specialist's basic assumption hold?

**Outputs a confidence-tagged verdict:**

| Verdict | Meaning | Next node |
|---|---|---|
| `known_confident` | Classifier + fingerprint + canonical test all agree | `plan` with specialist prompt |
| `known_similar` | Domain matches but canonical test fails (e.g., GitHub Enterprise variant) | `explore` (Tier 2) |
| `unknown` | No prior match | `explore` (Tier 2) |
| `unknown_hostile` | DOM is canvas/iframe-heavy; text won't ground planning | `explore_visual` (Tier 3) |

### 1.2 New node: `explore`

Bounded task-guided exploration. Triggered only when `recognize`
demotes. Budget: 60 seconds of wall time, max 5 pages visited.

**How it works:**
- Feeds the natural-language task + first input row to Opus.
- Asks "which 3 links/buttons on this page best match the task
  intent?" (task-guided, not zero-shot).
- Visits each; snapshots each.
- Produces a **platform profile**: motor programs like "to reach detail
  view, click a row element whose text matches `{{input.ticket_id}}`."
- Caches the profile keyed by `(host, UI fingerprint, DOM schema
  fingerprint)`.

Profile is then passed to `plan` as grounding context. Planner is no
longer zero-shot; it has concrete motor programs specific to this site.

### 1.3 New node: `explore_visual` (Tier 3 fallback)

When DOM is canvas-heavy (Tableau, analytics dashboards, image-based
PDFs), text grounding fails. This node:

- Overlays numbered Set-of-Mark boxes on rendered screenshots.
- Feeds screenshots to the planner as primary input; DOM as secondary.
- Produces a plan whose `act` steps reference mark IDs (not CSS
  selectors).
- Accuracy guard: every visual click must emit a `verification_text`
  for the tool dispatcher to match against inner text (Gemini fix #3).

### 1.4 Mandatory gate — the self-test

When `explore` or `explore_visual` builds a fresh profile, we DO NOT
trust it for 1000 samples yet. One extra flow gates the cache:

```
explore/explore_visual
         ▼
    plan (with new profile)
         ▼
    run sample 0 end-to-end
         ▼
    extract → judge → verdict
         ▼
   verdict = pass?
         ├── yes → cache profile, unlock remaining samples
         └── no  → demote to teach mode (Tier 4)
```

Teach mode: dashboard prompts the operator to walk through sample 1 in
a live browser; we record the trace; Opus abstracts it into a
parameterized motor program; replay on sample 2 for validation; only
then cache.

### 1.5 Fallback ladder summary

| Tier | Trigger | Source of plan | Cost |
|---|---|---|---|
| 1 — Known specialist | `recognize=known_confident` | Specialist prompt + plan cache | Cheapest, fastest |
| 2 — Explored profile | `recognize ∈ {known_similar, unknown}` | Task-guided exploration → profile → plan | +60s per new host once |
| 3 — Visual-first | `recognize=unknown_hostile` | Screenshots + mark IDs | +vision tokens per step |
| 4 — Teach mode | Tier 2 or 3 self-test fails | Human demo → abstracted trace | Requires human time once |
| 5 — Manual | Tier 4 replay fails | Human per sample | Fallback only |

### 1.6 What each tier actually reads from the page

Every tier has to answer two questions about the current page:

- **What can I click / type into?** (interactive elements)
- **What is data I should read?** (non-interactive content)

Different tiers answer these using different page representations. Here
is what each one reads, and how it tells buttons from data.

#### Quick map

| Tier | Primary representation | Secondary | Where "clickable" comes from | Where "data" comes from |
|---|---|---|---|---|
| 1 — Known specialist | Structured snapshot (text outline + interactive list) | Specialist prompt hints per platform | `interactive` list from snapshot | `inner_text` scoped to located node |
| 2 — Explored profile | Same structured snapshot + profile motor programs | Screenshot as sanity check | Motor program's `selector_hint` + interactive list | Motor program's locate hint + deterministic parsers |
| 3 — Visual-first | Full screenshot with Set-of-Mark boxes | DOM text where available | Set-of-Mark box IDs (drawn only on interactive els) | LLM reads pixels + cites bounding region |
| 4 — Teach mode | Human-recorded trace | Screenshot + DOM at each step | Recorded click target (selector + text) | Recorded extraction target (selector + text), parameterized |
| 5 — Manual | Human does it in the UI | Evidence screenshots only | N/A | Human types into the schema |

#### The structured snapshot (Tiers 1 + 2)

This is what `BrowserTools.snapshot()` returns today in the agent. It is
not raw HTML, not the full DOM tree, not a naive screenshot. It is a
curated projection designed for LLM consumption:

- **`url`** — the current page URL.
- **`title`** — the document title.
- **`inner_text`** — visible text content of the page body, with
  invisible and off-screen elements filtered out. This is the DATA
  surface. Whitespace collapsed; no HTML tags.
- **`outline`** — a compact structural sketch. Like a table of contents:
  what headings exist, what sections, how deep. Derived from heading
  tags + ARIA landmarks. Tells the planner the shape of the page
  without showing every `<div>`.
- **`interactive`** — the CLICKABLE / FILLABLE surface. A list of
  elements the agent can act on. Each entry has:
  - An element `role` (button / link / textbox / menuitem / …)
  - An accessible `name` (from `aria-label`, visible text, or `alt`)
  - A stable `selector` (CSS or role-based) the click tool can use
  - The `tag` (backup signal)
- **`page_state`** — readiness flags (DOM idle, network idle, loading
  indicators gone).

So the answer to "how do we separate button from data" on DOM-readable
sites: **we don't rely on the LLM to guess.** We pre-compute the
interactive list at snapshot time using deterministic signals:

1. ARIA role matches an interactive role set.
2. HTML tag is `<button>`, `<a href>`, `<input>`, `<select>`,
   `<textarea>`.
3. Element has `onclick` / `onkeydown` / `tabindex`.
4. Computed style shows `cursor: pointer`.
5. Role inherits from a labeled interactive parent (menu items inside
   a listbox).

Any element passing these is interactive; the LLM sees it in the
`interactive` list. Everything else that has visible text is data; the
LLM sees it as `inner_text`. No guessing.

The extractor, when asked for a specific field, does two things:

1. **Locate** — ask the LLM which node in the snapshot contains the
   field. Output: a selector or a text anchor.
2. **Parse** — run deterministic code (dateutil, currency regex, email
   validator) against the located node's text.

This is why the extractor rarely hallucinates. It is not producing the
value from imagination; it is picking a DOM location and letting code
parse what's actually there.

#### The explored profile (Tier 2 adds this)

When the site is unknown, the planner has no priors about where things
live. The profile fills that gap by storing **motor programs** — recipes
the planner can turn into plan steps without zero-shot guessing.

A motor program entry looks (conceptually) like:

> **`to_reach_detail_view`** — action: click an element whose role is
> `row` or `link`, whose accessible name contains the task's
> `{{input.ticket_id}}` value. Verification text = the ticket_id.

The navigator uses this as grounding when it selects a target from the
`interactive` list. The motor program says "click the row matching your
input"; the interactive list tells you WHICH row candidates exist on
the current page; the tool dispatcher does the click and verifies text
match.

Motor programs also describe extraction anchors:

> **`assignee_field_locator`** — in the detail view, the assignee is
> in an element with ARIA role `text` that is a sibling of the label
> text "Assignee".

The extractor uses this to call its locate step deterministically
instead of searching the whole page.

Profiles are never freeform description. They are always
"action-or-locate + a selector pattern + a verification hint" tuples.
That's what makes them transferable across 1000 samples of one task.

#### The screenshot + Set-of-Mark overlay (Tier 3)

When the DOM is hostile — canvas-heavy dashboards, embedded viewers,
image-first UIs — the structured snapshot's `inner_text` is empty or
nonsensical. We fall back to visual grounding.

**What we render:**

- Take a full-page screenshot.
- Enumerate the interactive-looking elements using the same signals
  as above, plus a geometric pass that finds clickable regions in the
  canvas (if any accessibility data is exposed there).
- Draw numbered boxes on the screenshot, one per detected interactive
  element. "Set-of-Mark" = marked set of click targets.

**What the LLM sees:**

- The image with numbered boxes.
- A legend listing `{box_id, approximate label text OCR'd from inside
  the box, role if known}`.

**How "button vs data" is decided:**

- **Boxes** = click targets. Always. If something isn't boxed, it
  isn't clickable at this tier.
- **Everything else visible** = data. The LLM reads pixel text; for
  extraction, it must cite the bounding region it read from.

**Accuracy guards specific to visual:**

- Click actions require a `verification_text` matched against OCR of
  the target box before dispatch.
- Extracted values get a second-pass check: we crop the cited region
  and ask a second LLM call "does this crop contain the claimed
  value?" Disagreement → field flagged, not silently written.

This tier is slower (more vision tokens) and more expensive, but it is
the only path that works on pages where text-based grounding is
fundamentally unavailable.

#### The recorded trace (Tier 4)

Teach mode captures what the human did, exactly. Playwright records:

- Every navigation (URL, timestamp).
- Every click (CSS selector Playwright picked, coordinates, the text
  inside the clicked element).
- Every type (selector, value).
- A screenshot before and after each action.
- The full DOM state at each step.

There is no ambiguity about button vs data at capture time: the human
physically distinguished them. The LLM's job is only to ABSTRACT the
trace so it generalizes:

- Replace literal values ("TICK-001") with input-field references
  (`{{input.ticket_id}}`).
- Keep selectors verbatim where stable (role-based, text-based).
- Keep verification text so later replays can confirm they clicked
  the right thing.

Extraction targets in teach mode are captured as "the node the human
stopped to highlight / copy / read" — we actually instrument the
teaching session so the operator indicates which value on screen is
the answer for which schema field.

#### Summary — what the agent sees vs what the user sees

The agent never "looks at the page" like a human. At every tier, it
looks at a **curated, typed representation** designed so that
"clickable" and "data" are distinct inputs the LLM consumes separately.

- Tier 1–2: pre-computed `interactive` list vs `inner_text`.
- Tier 3: pre-drawn boxes vs everything-else pixels.
- Tier 4: recorded click targets vs recorded extraction targets.

The LLM never has to answer "is this a button?" at inference time. The
representation answers that question before the prompt is built. This
is the single biggest accuracy lever in the system — and it is the
reason our agent doesn't fall apart the way most zero-shot agents do.

---

### 1.7 How this changes the state machine

Only the entry side. The loop (act/observe/verify/extract/judge) is
unchanged.

```
START → classify → recognize →┬─ known → plan ───┐
                              │                   │
                              ├─ similar ──┐      │
                              ├─ unknown ──┼→ explore →┐
                              │             │           │
                              └─ hostile ──┼→ explore_visual
                                            │           │
                                            │           │
                                            └──→ plan (with profile)
                                                        │
                                    [self-test gate for fresh profiles]
                                                        │
                                                        ▼
                                     [existing loop: act → observe → verify → extract → judge → END]
```

### 1.8 Cache semantics (matters for correctness)

- Specialist plan cache (existing): key = `(prompt, schema, url_pattern)`.
- Platform profile cache (new): key = `(host, UI_fingerprint, DOM_schema_hash)`.
- Every `observe` checks `DOM_schema_hash` against cached value;
  mismatch → invalidate profile → pause group → re-run `explore`.
- Drift is detected BEFORE the stale profile corrupts the next sample,
  not after downstream failures accumulate.

---

# Problem 2 — Authentication (simplified)

## The simple model we commit to

- **Once per host, a human runs `andera login`.** Real Chromium opens.
  They do the full dance — password, OAuth, SSO, MFA, whatever the
  site requires. We save the resulting browser session as an encrypted
  blob keyed by host.
- **That blob is bound to an IP from our proxy pool** at save time.
- Every agent sample for that host loads the blob into a fresh context
  **and uses the bound IP**. Site sees one continuous session from one
  IP.
- Session dies → we alert the operator to re-run `andera login`. That's
  the only failure mode we treat.

No need to distinguish password vs OAuth vs SSO in the agent code. The
`andera login` browser handles whatever the site presents; we just
capture the end state.

## Where auth fits in the graph

Auth is NOT a node. It's a precondition for the entire agent run on a
host. Handled at sample-start by `BrowserSession` construction:

```
Before sample runs:
  1. Queue says "process sample S on host H"
  2. Pod acquires the (host H, credential C) auth-group lease
  3. Pod gets: auth blob from content-addressed store
                IP from Redis proxy lease bound to that auth blob
  4. Pod opens Chromium context with (proxy = bound IP, storage_state = blob)
  5. LangGraph starts on that context: classify → recognize → plan → ...
```

One new in-graph touchpoint: **`auth_check` runs after every `observe`.**

## The auth_check node

**Why:** sessions die mid-run (idle timeout, site-enforced kill, MFA
step-up on sensitive action). If we don't detect this, we extract the
login page's HTML as data.

**What it does:** reads the just-captured snapshot, asks Haiku one
cheap yes/no question: "Does this page show authenticated content, or
is it a login wall?"

**Transitions:**

| auth_check verdict | Next node | Effect |
|---|---|---|
| authed | verify (normal flow) | no change |
| login_wall | END_ABORT with status=auth_died | pod releases lease; group parks; operator alert |
| rate_limited_screen | verify with `last_tool_error=rate_limit` | triggers existing retry + AIMD back-off |

The `observe → auth_check → verify` chain is the new inner loop.

## Multi-sample on same host

All samples for the same (host, credential) flow through the SAME auth
group, pull from the SAME Redis sub-queue, use the SAME session blob
and SAME IP. One login serves all 1000 samples.

Inside one pod, we open N contexts on the same Chromium. Each context
is independent (own cookies snapshot after loading blob, own page stack)
but all share the pod's pinned proxy IP. From the site's view: one
IP with many parallel tabs. Looks like a power user with tabs open.

## Multi-pod on same host

Only **one** pod at a time holds the auth-group lease. Other pods work
on OTHER (host, credential) groups. This is the key invariant: one
session lives on one IP on one pod. Violating it kills the session.

If pod A holding Workday crashes:
1. Lease TTL expires (~30s).
2. Pod B acquires the lease.
3. Pod B loads the SAME session blob AND picks up the SAME proxy IP
   (the proxy lease stays in Redis, not bound to the pod).
4. Samples resume from their LangGraph checkpoints.
5. Site sees no IP change, no session disruption.

## Multi-pod on different hosts

Full parallelism. Pod A runs Workday samples. Pod B runs GitHub samples.
Pod C runs Linear samples. Each with its own session, own IP, own
auth group. No coordination needed beyond the shared queue.

## What we tell the operator up front

- Sites needing push-MFA or recurring CAPTCHAs cannot run unattended
  past the first session death. Flagged as `interactive_required`.
- Sites with strict session-IP binding are Q4 quadrant; their per-sample
  wall time is bounded by per-session concurrency (see Problem 3).
- Sites with MFA via TOTP (code from authenticator app) can
  auto-refresh if the operator provides the TOTP seed at `andera login`
  time.

---

# Problem 3 — Speed across samples, within samples, pods, and rate limits

## The speed mental model

Four levers. Each has its own ceiling.

| Lever | Where it works | Ceiling |
|---|---|---|
| **Per-step latency** | Within one sample | Stability wait + LLM call time |
| **Parallelism across samples on one pod** | Multiple contexts per pod | Pod RAM + Chromium cap |
| **Parallelism across pods** | Scheduler fans out samples | Domain RPS + per-session concurrency |
| **IP diversity** | No-auth hosts only | Site's per-IP limit × pool size |

The lever you can pull depends entirely on whether the host is auth or
no-auth. The full story:

## 3.1 Within a single sample — serialized by necessity

A 6-step sample (`goto → click → filter → click → extract → done`) is
6 iterations of `act → observe → verify`. State flows linearly. Each
step needs the DOM state the prior step produced. There is no way to
parallelize within a sample.

Speed gains here are on **per-step latency**:

- **Plan cache HIT** = first sample of a task builds the plan; samples
  2..N reuse it. Removes Opus call per sample.
- **Classify memo HIT** = first sample calls Haiku; all others look up
  in a closure dict. Removes Haiku call per sample.
- **Stabilization wait** is fixed cost per step (≤ 1500ms worst case).
  Not optimizable without losing accuracy.
- **Navigator verify** is one Sonnet call per step. Cannot be skipped —
  it's the accuracy guard on misclicks.
- **Extract** is one Haiku call per sample (plus up to 2 retries on
  schema fail).

For a 6-step sample: ~6 × (page interaction + stability + Sonnet
verify) ≈ 8-12 seconds. This is the sample-wall-time floor.

**Optimization: within-sample LLM parallelism where safe.**
The judge-feedback re-extract (see `AGENT_ARCHITECTURE.md` §11) can run
while the next sample is already starting — judge's result for sample N
doesn't block pod's acquisition of sample N+1.

## 3.2 Across samples on one pod — context multiplexing

One pod can run multiple samples in parallel via Playwright browser
contexts. Each context is one independent sample. How many?

For **no-auth hosts**:
- Bounded by pod RAM (each Chromium context ≈ 150-300MB).
- Typical: 10-20 contexts per pod.
- No session constraint.

For **auth hosts** (lenient or strict):
- Bounded by **per-session concurrency cap** first, pod RAM second.
- Typical Workday: 5. Typical LinkedIn: 2. Typical GitHub: 30+.
- Running more contexts than the cap → site throttles or kills session.

## 3.3 Across pods — the scheduler and its ceiling

We run N agent pods. Each dequeues samples from Redis. But dequeue is
NOT blind — it respects auth-group leases.

**For no-auth samples:** any pod can run any sample. No lease needed.
Max parallelism = `pods × contexts_per_pod`.

**For auth samples:** only the pod holding that (host, credential)
group's lease can process its samples. Max parallelism per group =
`contexts_per_pod` (bounded by site's per-session cap).

The per-group ceiling is the key honest number. Example:

- 1000 samples, host = Workday, credential = 1 account, site cap = 5.
- Max concurrent = 5, no matter how many pods we run.
- Wall time = (1000 × 15s) / 5 ≈ **50 minutes**.
- Adding pods doesn't help. Adding Workday accounts doubles/triples
  throughput.

For a mixed run — 500 Workday + 500 GitHub:
- Workday group: ceiling 5.
- GitHub group: ceiling 30.
- Both run in parallel on different pods.
- Wall time = max(Workday wall, GitHub wall) = max(25min, 4min) ≈ 25min.

## 3.4 Domain-level rate limiting

Every outbound HTTP request goes through a Redis-backed token bucket
before leaving the pod. The bucket is **cluster-wide** — 10 pods making
requests to `github.com` coordinate through one bucket per (host,
endpoint_class).

**Why per-endpoint-class, not per-host:** sites rate-limit `/search`
tighter than `/issues`. One bucket per class prevents search bursts
from starving normal navigation.

**How the ceiling is set:** AIMD. Start at a conservative floor (say
0.5 rps). Every minute without a rate-limit signal, raise ceiling by
25%. On any 429, Cloudflare challenge, or "slow down" banner, halve
ceiling immediately. Converges on the site's real tolerance in 2-3
minutes without manual tuning.

**Where this plugs into the agent:** at the `BrowserSession` layer
(Playwright route hook), BEFORE the request leaves. The agent's `act`
node doesn't know about rate limits — the session transparently waits
for a token.

## 3.5 User-level / session-level rate limiting

Separate dimension. The site applies a per-session concurrent-requests
cap (Workday ~5, LinkedIn ~2). This is enforced at context creation
time: we don't open more than `per_session_cap` contexts per auth-group
lease, regardless of pod RAM or domain bucket state.

Encoded in credentials config per host. Respected by scheduler at
lease acquisition.

## 3.6 IP strategy by host class

| Host class | IP strategy | Why |
|---|---|---|
| **No-auth, simple** | Rotate per context | Anti-detection; no session to protect |
| **No-auth, Cloudflare-protected** | Warm pool of 10-20 stable IPs | Each new IP pays a 2-5s challenge; rotation burns throughput |
| **Auth, lenient** (GitHub PAT) | One sticky IP per session | Token-based auth is IP-independent but flagged on excessive fan-out |
| **Auth, strict** (Workday, bank) | One sticky IP per session, held by proxy lease | Session-kill on IP change |

The classification is decided at `recognize` (§1.1 above). Passed to
`BrowserSession` at context creation.

## 3.7 The full speed picture for common scenarios

| Scenario | Parallelism | Wall time (1000 samples @ 15s) |
|---|---|---|
| 1000 no-auth public-data samples | 50 contexts across pods | ~5 min |
| 1000 Workday samples, 1 account | 5 | ~50 min |
| 1000 Workday samples, 3 accounts | 15 | ~17 min |
| 1000 GitHub samples, 1 PAT | 30 | ~8 min |
| 500 Workday (1 acct) + 500 GitHub (1 PAT) | 5 + 30 in parallel | max(25min, 4min) = 25 min |
| 1000 samples, unknown platform with auth | 5 (strict-like default) + 60s exploration overhead | ~52 min |

## 3.8 What we CANNOT make faster

- One sample with 7 serial steps cannot be parallelized internally.
  Sample wall time has a floor.
- One authed session cannot exceed its site's per-session cap.
  More pods does not help.
- A cold AIMD on a new host burns 1-2 minutes discovering the
  ceiling. Pre-seed from prior run to skip.

---

# 4. Putting it all together — scenarios

## Scenario A: 1000 public GitHub samples

```
recognize → known_confident (GitHub)
         → plan (specialist, cached on sample 0)
         → [act/observe/verify]×6 → extract → judge
Auth:    none (no login needed)
Parallelism: 50 contexts across 5 pods, rotating IPs
Rate limit: AIMD discovers ~30 rps on github.com
Wall time: ~5 min
```

## Scenario B: 1000 Workday, 1 user account

```
recognize → known_confident (Workday) [after canonical test passes]
         → plan (specialist, cached)
         → [act/observe/verify]×N → extract → judge
Auth:    `andera login` once → encrypted blob bound to proxy IP
Parallelism: 5 contexts on 1 pod holding Workday lease
Rate limit: AIMD + per-session cap of 5
Wall time: ~50 min (ceiling is the account, not the infra)
Failure: push-MFA triggers → agent alerts; operator reauths; resume
```

## Scenario C: 1000 samples, unknown customer portal

```
recognize → unknown (no match)
         → explore (task-guided, 60s)
         → plan (with fresh profile)
         → RUN SAMPLE 0 → judge → pass?
              yes → cache profile, unlock 999
              no  → teach mode (human walks sample 1)
Auth:    if probe says login needed → `andera login` once
Parallelism: 5 contexts (default strict until proven lenient)
Wall time: 60s exploration + canary (5 samples × 15s) + 995 × 15s / 5 ≈ 52 min
```

## Scenario D: 1000 samples across 4 mixed hosts

```
Scheduler partitions by (host, credential):
  host-A (Workday, 1 acct):  500 samples, cap 5  → ~25 min
  host-B (GitHub PAT):       300 samples, cap 30 → ~2.5 min
  host-C (public site):      150 samples, 50 ctx → ~0.75 min
  host-D (unknown):          50 samples, explore + run → ~3 min

All 4 groups run in parallel on different pods.
Wall time = max of group times = ~25 min.
```

---

# 5. What the state machine looks like after all additions

The existing loop (act/observe/verify → extract → judge) is unchanged.
Two new nodes at the entry; one new node inside the loop:

```
START
  │
  ▼
classify            (existing — picks task_type)
  │
  ▼
recognize           (new — picks platform tier)
  │   ┌──────────────────────────────┐
  ▼   ▼                              │
plan   explore / explore_visual      │
  │     │                            │
  │     ▼                            │
  │   self-test gate                 │
  │     │ pass                       │
  ◄─────┘                            │
  ▼                                  │
act ──┐                              │
  │   │                              │
  ▼   │                              │
observe                              │
  │                                  │
  ▼                                  │
auth_check          (new — detects login wall mid-run)
  │
  ▼
verify
  │   (loop to act, or escalate to plan, or proceed to extract)
  ▼
extract
  │
  ▼
judge
  │
  ▼
END
```

**Out-of-graph additions (infrastructure, not nodes):**
- Rate limiter (Redis token bucket) on every outbound request.
- Proxy pool (Redis lease) tied to auth blob.
- Scheduler (auth-group queues + fair lease acquisition).
- Platform profile cache (content-addressed by host + fingerprint).

These are handled at the `BrowserSession` and queue layers. The agent
nodes don't know about them. The hexagonal architecture already in place
(every external dep behind a Protocol) means these slot in without
touching node code.

---

# 6. The non-negotiables

Regardless of which scenario we're in, these always hold:

- **No plan runs on 1000 samples without a self-test on sample 0.** If
  exploration built the plan, sample 0 must pass schema + judge before
  the rest queue.
- **`auth_check` runs after every `observe`.** Login wall detected → abort
  sample, alert operator. No scraping login-page HTML as data.
- **DOM fingerprint is checked every `observe`.** Drift → invalidate
  profile → pause group → re-explore. No sample runs against a stale
  profile.
- **One session lives on one IP on one pod.** Session fan-out is how
  sessions die. Proxy-held leases enforce this across pod failures.
- **Domain rate limit is shared across pods via Redis.** Per-pod
  throttling is theater when 10 pods hit the same target.
- **Per-session concurrency cap is respected.** Exceeding it is how
  strict sites kill sessions silently.
- **Everything is audit-logged.** Which profile ran which sample,
  which IP owned which session, when auth died, when the rate limiter
  backed off. Auditors can trace any output back to its decisions.

Accuracy comes first. Speed is maximized inside the constraints that
accuracy imposes, never at their expense.
