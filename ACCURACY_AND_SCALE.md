# Accuracy & Scale — Full Solution Document v2

*Supersedes v1. Incorporates self-review of auth/proxy/parallelism design AND
review of unknown-platform planning.*

*Design principle: **maximum speed, never at the expense of accuracy.** Every
speed move has a documented accuracy guard. Audit-grade means: if the system
can't be accurate, it says so loudly rather than shipping wrong data fast.*

*Covers every concern from `todo.txt` (auth, unknown platforms, rate limiting,
IP rotation, tree-into-LLM) plus the five Gemini suggestions plus distributed-
systems realities (pod death, fairness, multi-host samples, proxy outages).*

---

## 0. What changed from v1

| Area | v1 | v2 |
|---|---|---|
| Auth scope | per-host flag | **per-URL-pattern** attribute discovered via rendered probe |
| Session recovery | "pod owns it" | **proxy-held lease** + LangGraph replay; any pod can take over |
| Rate limiting | per-host token bucket | **AIMD controller** + multi-dimensional buckets (host × endpoint-class) |
| Proxy strategy | one policy | **4-quadrant policy** (auth × site-tolerance) |
| Unknown platforms | build + cache profile | **probe → auth → explore → self-test → canary → cache** with DOM fingerprint |
| Multi-host samples | not covered | explicit primary/secondary host binding |
| Failure recovery | ad hoc | **explicit recovery matrix** |
| Speed/accuracy | implicit | **explicit tradeoff matrix** with guards |

---

## 1. The four-quadrant mental model

Every host we touch falls into one of four quadrants. The axes are orthogonal
and each has different mechanics. ALL other sections in this doc are organized
by this matrix.

```
                    ┌─────────────────────────────────────────────┐
                    │                                             │
                    │   Q3: Auth + Lenient                        │
                    │   - GitHub API+PAT, Linear                  │
                    │   - sticky IP (warning: not strict)         │
                    │   - parallelism via contexts (10-20)        │
                    │   - session tokens tolerate some IP fan-out │
                    │                                             │
    AUTH     ──────►├─────────────────────────────────────────────┤
                    │                                             │
                    │   Q4: Auth + Strict                         │
                    │   - Workday, LinkedIn, Salesforce, banks    │
                    │   - sticky IP HARD; proxy-held, not pod-held│
                    │   - parallelism per-session cap (3-10)      │
                    │   - session death = re-login alert          │
                    │                                             │
                    └─────────────────────────────────────────────┘
                    ┌─────────────────────────────────────────────┐
                    │                                             │
                    │   Q1: No-auth + Simple                      │
                    │   - public docs, APIs, blogs                │
                    │   - rotate IP per context                   │
                    │   - parallelism = pods × contexts           │
                    │   - bounded only by site aggregate RPS      │
                    │                                             │
    NO AUTH  ──────►├─────────────────────────────────────────────┤
                    │                                             │
                    │   Q2: No-auth + Challenge-first             │
                    │   - Cloudflare/Akamai protected sites       │
                    │   - warm IP pool; rotation BURNS throughput │
                    │   - parallelism = IP pool size × contexts   │
                    │   - first hit on new IP pays 2-5s challenge │
                    │                                             │
                    └─────────────────────────────────────────────┘
                         SIMPLE      ─────►      CHALLENGE-FIRST
```

Quadrant assignment per host is **discovered**, not declared, and cached in
Redis with TTL. See §2.

---

## 2. Authentication — per-URL, probe-based, recoverable

### The full problem

- Auth methods: password, OAuth redirect, SSO/SAML, TOTP, push-MFA, CAPTCHA,
  device trust, API tokens, session cookies.
- Auth is **per-URL-pattern**, not per-host (GitHub: public vs private).
- Sessions die from: idle timeout, absolute timeout, concurrent-session kill,
  step-up MFA, admin kill, IP change, fingerprint change.
- Multi-pod cluster: auth was done on pod A, samples run on pod B.
- 1000 samples on one user's account cannot saturate a 40-pod cluster.

### Solution — credential vault + per-URL probe + proxy-held sticky IP

#### 2.1 Credential declaration (operator)

```yaml
# config/credentials.yaml  (gitignored; secrets via env refs)
hosts:
  github.com:
    surfaces:
      - url_pattern: "^https://github\\.com/(?!.*/settings).*"
        auth: optional                 # public repos
      - url_pattern: ".*"
        auth: required
        strategy: pat_header
        secret_ref: GITHUB_PAT

  workday.myco.com:
    surfaces:
      - url_pattern: ".*"
        auth: required
        strategy: storage_state
        blob_ref: data/credentials/workday-myco.bin
        sticky_ip: hard                # proxy-held lease, mandatory
        per_session_concurrency: 5     # site cap
        session_ttl_min: 30
        refresh_strategy: interactive  # human must re-run `andera login`

  linkedin.com:
    surfaces:
      - url_pattern: ".*"
        auth: required
        strategy: storage_state_plus_totp
        blob_ref: data/credentials/linkedin.bin
        totp_ref: LINKEDIN_TOTP_SEED
        sticky_ip: hard
        per_session_concurrency: 2
        refresh_strategy: programmatic # re-login using stored creds + TOTP

  unknown.example.com:
    # no entry → auth treated as `unknown`; probe discovers
```

#### 2.2 Auth probe (run-start, parallel across hosts)

**NOT** a raw HTTP GET — cloaked sites serve different HTML to bots.

```python
async def probe_auth(url: str) -> AuthProbeResult:
    async with throwaway_chromium(proxy=pool.get_clean_ip()) as ctx:
        page = await ctx.new_page()
        await page.goto(url, wait_until="networkidle")
        await wait_for_stable(page)                     # §8
        snap = await snapshot(page)                     # screenshot + inner_text + outline
        verdict = await haiku_classify(snap, prompt=AUTH_PROBE_PROMPT)
        # verdict ∈ {authed_visible, login_wall, sso_redirect, captcha,
        #            rate_limited, geo_blocked, error}
    return AuthProbeResult(url, verdict, dom_fingerprint=snap.dom_hash)
```

Cache in Redis: `probe:<host>:<url_class> → (verdict, dom_hash, ts)` with 6h TTL
for quick invalidation on site changes. Parallel at run-start for all unique
hosts — NOT lazy on first sample.

#### 2.3 Sealed-state + proxy-held lease (recovery-correct)

Classic SPOF fix: **the sticky IP belongs to the PROXY LEASE in Redis, not to
the pod**. When pod crashes, the proxy id + sealed-state stay reserved; a warm
standby pod acquires the same lease and continues.

```
Redis keys:
  auth_lease:<auth_group_id>:proxy_id          → "prox-042"
  auth_lease:<auth_group_id>:pod_id            → "agent-3" (heartbeat, TTL=30s)
  auth_lease:<auth_group_id>:sealed_blob_ref   → "data/credentials/workday.bin"
  auth_lease:<auth_group_id>:last_refresh      → epoch
```

Pod dies → pod_id TTL expires in 30s → warm pool's next pod sets pod_id,
loads the SAME sealed state, uses the SAME proxy_id (same egress IP), resumes
samples from their LangGraph checkpoints. The site sees **one continuous
session from one IP**. No session kill.

#### 2.4 Auth-check node (per sample step)

```python
# LangGraph node wired after every observe
async def auth_check(state):
    verdict = await cheap_auth_classifier(state.last_snapshot)
    if verdict == "authed":
        return state
    elif verdict == "login_wall" and group.refresh_strategy == "programmatic":
        await refresh_session(state)       # fills form, TOTP if needed
        return state.retry_step()
    elif verdict == "captcha" or verdict == "push_mfa":
        alert_operator(group, verdict)
        return state.park_group()          # stops dequeue for this group
    elif verdict == "rate_limited":
        aimd.on_limit(state.host)          # §3
        return state.backoff_and_retry()
```

The classifier is Haiku, ~$0.0005/call. Memoize by URL-class (same URL + same
DOM hash → cached verdict for 60s) to cut cost on multi-step samples.

#### 2.5 Speed moves (with accuracy guards)

| Speed move | Gain | Accuracy guard |
|---|---|---|
| Probe in parallel for all hosts at run-start | Removes per-host 30s serial cost | Probe result hashed by dom fingerprint; stale cache auto-invalidated |
| Skip probe for hosts with fresh cache | No probe cost for repeated runs | TTL 6h; invalidate on any auth_check failure |
| Memoize auth_check by (url_class, dom_hash) | 60% fewer LLM calls on multi-step samples | Cache TTL 60s; invalidate on any action that posts |
| Warm-pool pods pre-loaded with sealed state | Zero cold-start on pod failover | Lease fence token prevents two pods thinking they own it |

### What this costs

- Probe budget: ~$0.002 per unique host per 6h + 2s of wall time.
- Classifier: ~$3 per 1000-sample run.
- Warm-pool pods: 1 spare per active auth_group (cheap; ~300MB each).
- `manual_required` hosts (push-MFA, CAPTCHA) can't be automated — call out up front.

---

## 3. Rate limiting — AIMD + multi-dimensional buckets

### The full problem

- Site aggregate RPS is **unknowable up-front**.
- Rate limits apply on at least 4 axes: per-IP, per-account, per-endpoint-class,
  concurrent-request cap.
- In-process per-agent limiters don't coordinate → cluster-wide rate violation.
- Burning 429s = burning IP reputation and/or triggering bans.

### Solution — cluster-coordinated AIMD on multi-dimensional buckets

#### 3.1 Bucket schema (Redis)

```
rate:<host>:<endpoint_class>:tokens       INCR/EXPIRE, cluster-wide
rate:<host>:<endpoint_class>:max_rps      current AIMD-tuned ceiling
rate:<host>:concurrency:inflight          per-host concurrent cap
rate:<host>:last_429                      timestamp, triggers decrease
```

Endpoint classes extracted via URL regex per host (defaulted; operator-overridable):

```yaml
rate_limits:
  github.com:
    endpoint_classes:
      search: { pattern: "/search/", max_rps_initial: 0.5 }
      api:    { pattern: "/api/",    max_rps_initial: 5 }
      default:                        { max_rps_initial: 2 }
    concurrency_cap: 20
```

#### 3.2 AIMD controller

**Additive Increase, Multiplicative Decrease** — the same algorithm TCP uses.
Proven to find a fair share of an unknown-capacity resource.

```python
# Called from the Playwright route() hook BEFORE every request
async def acquire_token(host: str, url: str) -> None:
    bucket = classify_endpoint(host, url)
    key = f"rate:{host}:{bucket}:tokens"
    while True:
        ok = await redis.eval(TOKEN_BUCKET_ACQUIRE, key, max_rps_current)
        if ok: return
        await asyncio.sleep(0.05)

# Called when we SEE a response
async def on_response(host, bucket, status):
    max_key = f"rate:{host}:{bucket}:max_rps"
    if status == 429 or status in RATE_LIMIT_SIGNALS:
        # multiplicative decrease
        await redis.eval(MULT_DECREASE, max_key, 0.5, MIN_RPS)
    elif successful_for(60):
        # additive increase, once per minute
        await redis.eval(ADD_INCREASE, max_key, 0.25, MAX_RPS_CAP)
```

Starts conservative (floor 0.5 rps, say), ramps up on sustained success,
halves on any 429. Settles near the site's real ceiling within a few minutes.

#### 3.3 Challenge/CAPTCHA detection feeds the same controller

Not just 429. Cloudflare challenge pages, rate-limit banners in HTML, forced
re-CAPTCHA — all trigger `on_limit()`. Detected by a cheap classifier in the
response-body hook.

#### 3.4 Speed moves

| Speed move | Gain | Accuracy guard |
|---|---|---|
| Start at discovered ceiling from prior run (persist `max_rps` across runs) | No ramp-up time on repeat | Decay by 50% if last run was >24h ago |
| Parallel endpoint-classes (search doesn't block api) | Higher throughput on mixed workloads | Separate buckets, no cross-class borrowing |
| Burst budget (brief 2× overage allowed, paid back) | Handles bursty multi-step samples | Cluster-wide token count caps total |

### What this costs

- ~10ms Redis roundtrip per request. Negligible vs page load.
- Some 429s in the first minute of a new host. Acceptable; that's how AIMD
  finds the ceiling.

---

## 4. Proxies — 4-quadrant policy

### The problem

One proxy policy doesn't fit all quadrants. Sticky-per-session breaks
anti-detection on simple sites. Rotating breaks auth AND burns throughput on
challenge-first sites. Proxy vendor outages take everything down.

### Solution — per-host policy discovered + circuit-breakered pool

#### 4.1 Policy by quadrant

| Quadrant | Strategy | Rotation trigger |
|---|---|---|
| Q1 No-auth + simple | `rotate_per_context` | every new context |
| Q2 No-auth + challenge | `warm_pool` (N stable IPs) | only after burn signal |
| Q3 Auth + lenient | `sticky_per_session` | session end / TTL |
| Q4 Auth + strict | `sticky_proxy_held` | ONLY on session death |

Assigned at probe time; overridable via `profile.yaml`.

#### 4.2 Pool schema

```
proxy:pool:all                        SET of all proxy ids
proxy:<id>:meta                       {url, type, regions, cost_per_gb}
proxy:<id>:lease                      current holder (auth_group or context id) + TTL
proxy:<id>:host_cooldown:<host>       TTL key; blocks reuse on host
proxy:<id>:health                     {success_count, failure_count, last_error}
proxy:<id>:circuit                    {state: closed|open|half_open, open_until}
```

#### 4.3 Pool sizing requirement

For quadrant Q3/Q4 to not starve:
```
|proxies for host| >= ceil(max_parallel_groups × session_ttl / cooldown_min)
```

If pool runs dry: BLOCK. Do NOT reuse an IP past its cooldown; that's how
sessions die.

#### 4.4 Circuit breaker

Per-proxy: 3 consecutive failures → open for 60s. Provider-wide: 50% of pool
in failed state → **drain mode**: no new acquisitions, finish in-flight,
alert operator. No auto-failover to direct connect — that breaks auth and
reveals your infrastructure IP.

#### 4.5 Speed moves

| Move | Gain | Guard |
|---|---|---|
| Pre-warm N IPs per host at run-start (visit home page, cache challenge cookies) | Eliminates first-sample challenge cost on Q2 sites | Challenge cookies expire; re-warm every 30 min |
| IP geo-pinning to user's historical region | Avoids geo-challenge escalation | Only for auth sites where we have the hint |
| Pool bias toward recently-successful IPs | Reduces 429/challenge rate | Fairness rotation prevents monopoly; degrade if pool unbalanced |

---

## 5. Parallelism — pod death, session portability, fairness

### The problem

- Adding pods doesn't add throughput for an auth group; only contexts do.
- Pod crashing mid-run orphans 5 in-flight samples.
- One auth group with 1000 samples starves another with 10.
- One user account has a hard per-session concurrency ceiling.

### Solution — auth-group scheduler + proxy-held recovery + weighted fair queueing

#### 5.1 Scheduler model

```
auth_group(host, credential_id) owns:
  - one proxy lease (sticky IP)
  - one sealed credential blob
  - one Chromium instance + N contexts on any pod that currently holds pod lease
  - its own sub-queue in Redis

Cluster:
  - K pods, each can hold 1..M auth_group leases at a time
  - one warm-standby pod per active group (pre-loaded with likely blobs)
```

Dequeue protocol:

```python
async def next_sample(pod_id):
    # 1. Try groups we already hold leases for
    for gid in held_leases:
        sample = await redis.rpop(f"queue:group:{gid}")
        if sample: return sample
    # 2. Try to acquire a lease for a group with backlog
    for gid in await redis.zrange("groups_with_backlog"):
        if await try_acquire_lease(gid, pod_id, ttl=30):
            await load_group(gid)       # fetch blob, pin proxy
            return await redis.rpop(f"queue:group:{gid}")
    # 3. Idle
    return None
```

Leases have TTL=30s, extended by pod heartbeat every 10s. Pod death → 30s
max orphan window; warm standby picks up.

#### 5.2 Weighted fair queueing

Groups compete via deficit round-robin:

```
group_weight = sample_count_queued × urgency_multiplier
pick_next_group = DRR(groups_with_backlog, weights)
```

Prevents the 1000-sample group from monopolizing all leases while the
10-sample group waits hours.

#### 5.3 Per-session concurrency cap is the hard ceiling

```
per_group_parallelism = min(
    credentials[group.host].per_session_concurrency,    # site cap
    pool_max_contexts_per_chromium,                     # ~20 before OOM
    chromium_max_mem / context_mem                      # pod RAM cap
)
```

For one Workday account with `per_session_concurrency=5`, adding pods or
contexts does nothing past 5. Scale throughput by adding MORE ACCOUNTS (new
auth_group, new lease, new parallel track) or by accepting the wall-time
floor: `samples × time / 5`.

#### 5.4 Recovery matrix

| Failure | Blast radius | Recovery |
|---|---|---|
| Context crash | 1 sample | LangGraph checkpoint replay on fresh context, same pod |
| Chromium OOM | N contexts on that browser | Spawn new Chromium with same proxy+state; replay all affected samples |
| Pod crash | All groups held by pod | Leases TTL in 30s; warm standby takes over each; replay from checkpoints |
| Redis restart | All leases + buckets evaporate | Pause dequeue cluster-wide; resume after Redis back; rebuild buckets lazy |
| Proxy vendor outage | Everything egressing through vendor | Circuit-breaker opens; drain mode; alert; no direct-connect fallback |
| Site session kill (detected as login_wall mid-run) | All queued samples for group | Park group; `refresh_strategy` determines auto vs human re-login |

#### 5.5 Concrete throughput for 1000 samples

| Scenario | Per-group cap | Parallelism | Wall @ 15s/sample |
|---|---|---|---|
| 1 Workday account | 5 | 5 | **50 min** (hard floor) |
| 3 Workday accounts | 5 each | 15 | **17 min** |
| GitHub PAT (Q3) | 30 | 30 | **8 min** |
| 1000 samples across 5 unauth Q1 sites | n/a | pods × contexts | **4 min** at 50 contexts |
| 500 Workday (1 acct) + 500 Linear (1 acct) | 5 + 10 | 15 parallel | **~16 min** (max of group wall times) |

---

## 6. Multi-host samples

### The problem

Sample: "Read Linear ticket TICK-123 → search GitHub for related PRs → post
summary to Slack." Three hosts, three auth groups, one sample.

### Solution — primary host owns the lease, secondary hosts acquire sub-leases

```
sample.primary_host = first_host_in_plan           # determined by planner
sample.secondary_hosts = [host_2, host_3, ...]
```

Pod holding primary's lease ALSO acquires short-lived sub-leases for each
secondary at step-boundary. Sub-lease TTL matches sample's remaining
wall-time budget. Proxy binding per-host inside one sample.

Alternative rejected: decompose into chained samples via DSL. Too much
overhead for the 5-task trial scope; revisit at scale if cross-host samples
become common.

Accuracy guard: each host's auth_check node runs independently per host;
session death on host-2 doesn't poison host-1's state.

---

## 7. Unknown platforms — exploration with validation

### The problem (recap with review findings)

We can't require platform-specific config. System must work on in-house
ERPs, regional HRIS, vendor portals with zero training-data coverage.
Zero-shot LLM planning on these collapses. Previous v1 proposal had 5
critical accuracy gaps (reactive cache invalidation, no profile validation,
uncalibrated classifier confidence, no demotion path, reckless first-run
scale). This section fixes all of them.

### Solution — probe → auth → explore (task-guided) → self-test → canary → cache (DOM-fingerprint keyed)

#### 7.1 Pipeline stages (sequential gates)

```
Stage A — Probe (parallel at run-start, ~2s per host)
  ├─ quadrant: Q1/Q2/Q3/Q4 assignment
  ├─ auth_verdict: authed_visible / login_wall / captcha / ...
  └─ dom_fingerprint: hash(structural DOM markers)
        ↓
Stage B — Classify (~500ms per host)
  ├─ multi-signal: (domain, dom_fingerprint, branding_text, favicon_hash)
  └─ output: known_platform | unknown
        ↓
        ├─ IF known_platform AND canonical_action_test passes:
        │     → use specialist bundle, skip Stages C-F (fast path)
        │     (test: "open profile menu" or equivalent succeeds)
        │
        └─ IF unknown OR canonical test fails:
              ↓
Stage C — Auth (if required; blocks Stage D)
  └─ interactive `andera login` OR storage_state replay (§2)
        ↓
Stage D — Task-guided exploration (adaptive, 1-10 pages, budget 60s)
  ├─ Feed NL task + start_url + current snapshot to Opus
  ├─ "What 3 nav items best match the task intent? List with reasoning."
  ├─ Visit each; snapshot; add to corpus
  ├─ Stop when: new page adds <5% new DOM structural entropy OR budget exhausted
  └─ output: exploration corpus {url, screenshot, outline, interactive, dom_fp}
        ↓
Stage E — Profile synthesis (Opus, one call, corpus → JSON)
  ├─ page_taxonomy: {list_view: [urls], detail_view: [urls], form: [urls]}
  ├─ primary_nav: {selector_hint, items: [{label, url_pattern}]}
  ├─ exemplar_motor_programs:
  │     to_reach_detail_view:
  │       - action: click
  │       - selector_hint: "tr[role=row] a"
  │       - verification_text_source: "<task.input_data.ticket_id>"
  │     to_paginate:
  │       - action: click
  │       - selector_hint: "button[aria-label*=next]"
  │     to_filter:
  │       - action: fill
  │       - selector_hint: "input[type=search]"
  ├─ pagination_style: {type: "infinite_scroll|numbered|load_more"}
  ├─ timezone_hint: detected_via(page_text + user_profile + domain_tld)
  ├─ ui_fingerprint: {classic|lightning|v2|unknown}
  └─ dom_schema_hash: hash(stable structural markers)
        ↓
Stage F — Profile self-test (MANDATORY; gate)
  ├─ Run sample 0 (first row of input CSV)
  ├─ Extract per schema; validate jsonschema + field-type parsers
  ├─ If validation passes AND judge-model confidence ≥ 0.9:
  │     → cache profile, unlock canary batch
  └─ If fails:
        → do NOT cache
        → demote to tier-3 teach mode (§7.6) OR fail with clear message
        ↓
Stage G — Canary batch (5 samples, dashboard review optional)
  ├─ Run first 5 samples with full trace visibility
  ├─ Judge all 5; extract field-value distribution
  ├─ If 5/5 pass: unlock remaining 995
  ├─ If 3-4/5 pass: pause; surface samples to dashboard for human review
  └─ If <3/5 pass: invalidate profile; fall through to teach mode
        ↓
Stage H — Full run (parallel per §5)
  └─ Invalidation triggers:
        ├─ extractor schema-fail rate > 5% rolling window → re-Stage E
        ├─ dom_schema_hash on any page ≠ cached hash → re-Stage D
        └─ auth_check returns login_wall → re-Stage C
```

Each stage has a fallback. No single zero-shot LLM call between the operator
and 1000-sample execution.

#### 7.2 DOM-schema fingerprint (proactive cache invalidation)

```python
def dom_schema_hash(page_snapshot):
    # Stable structural markers — resilient to content changes,
    # sensitive to layout changes
    tokens = []
    for el in page_snapshot.interactive:
        tokens.append(f"{el.tag}:{el.role}:{el.aria_label_present}")
    tokens.sort()
    structure = page_snapshot.tag_sequence_sketch()   # nth-level DOM outline
    return sha256("\n".join(tokens) + structure)[:16]
```

At every observe snapshot, compute hash; compare to profile's
`dom_schema_hash`. Mismatch → invalidate profile BEFORE running the sample.
Proactive, not reactive.

#### 7.3 Task-guided exploration (not zero-shot nav picking)

```
Prompt to Opus:
  "Task: {nl_task}
   Task input example: {first_row_of_csv}
   Start page snapshot: {screenshot + outline + interactive}
   
   Question: Which nav items / URLs on this page are semantically closest
   to the task intent? Return up to 3, with one-sentence reasoning each."
```

The task constrains the search space. Prevents exploring Settings when the
task is about Reports.

#### 7.4 Profile stores motor programs, not descriptions

Descriptions ("this site uses infinite scroll") don't help the navigator
act. Motor programs do:

```json
"exemplar_motor_programs": {
  "to_paginate": [
    {"action": "scroll", "direction": "down", "until": "no new rows for 2s"},
    {"action": "fallback_click", "selector_hint": "button[aria-label*=more]"}
  ]
}
```

Navigator prompt includes this block verbatim when the current state matches
the motor program's trigger.

#### 7.5 UI fingerprint for same-host, multi-UI sites

Workday serves classic OR lightning based on settings. Profile key becomes
`(host, ui_fingerprint)`. Fingerprint detected per-session via lightweight
marker check (presence of specific CSS class / meta tag). Two profiles
coexist for `workday.myco.com`.

#### 7.6 Teach mode (Stage F fallback)

When self-test fails even with profile:

1. Dashboard shows: "This platform couldn't be auto-planned. Walk through
   sample 1 — we'll record your clicks and generalize."
2. Chromium opens non-headless; we record via Playwright trace.
3. LLM **abstracts** the trace: parameters (input values) become variables,
   selectors preserved as exemplars, page-transitions marked.
4. Abstraction is itself validated by replaying on sample 2 (different
   data, same pattern). If sample 2 matches schema: profile + abstracted
   trace are cached.
5. If sample 2 fails: surface to operator; do not unlock bulk run.

Abstraction prompt:

```
Trace: [{click: "#row-1234 a"}, {fill: "#search", value: "TICK-001"}, ...]
Task: "Find the assignee for each ticket in input CSV"
Input row example: {ticket_id: "TICK-001"}
Output: Produce a parameterized trace where literal values that match
        input fields become {{input.<field>}}.
```

#### 7.7 Speed moves for unknowns (with accuracy guards)

| Speed move | Gain | Accuracy guard |
|---|---|---|
| Parallel probe+classify for all unique hosts at run-start | -30s-per-host serial | Stage gates still enforced per-host |
| Cache profile keyed by `(host, ui_fingerprint, dom_schema_hash)` | Zero exploration cost on repeated runs | Hash mismatch invalidates; schema-fail rate triggers rebuild |
| Run Stage E + Stage F on CHEAPEST sample first (smallest row in CSV) | Faster self-test | Cheapness ≠ representativeness; Canary batch (G) catches this |
| Skip canary if profile is ≥7 days old with clean history | -5 samples of delay | If any prior run had >2% extractor failures, force canary |
| Parallel exploration across hosts | Wall time = max(per-host), not sum | Independent Opus calls; cost scales linearly |

Speed gained: on first encounter, unknown platform takes ~90s overhead
(probe 2s + explore 60s + synthesize 15s + self-test 15s). Canary adds 5
samples. After this, all 1000 samples run at normal throughput. Across a
10-run week on the same host: first run 90s overhead, runs 2-10 pay zero.

#### 7.8 Accuracy floor guarantee

**The system will NEVER silently produce wrong data for an unknown platform.**
One of these always happens:
- Profile self-test passes → proceed.
- Self-test fails → teach mode.
- Teach mode replay fails → human-required alert.
- Profile drifts mid-run → invalidate + rebuild with pause.

The only way to get wrong data out is if the human-provided ground truth
in Stage F is itself wrong (user error) — at which point the audit log
records every decision for review.

---

## 8. Visual & network stabilization (Gemini #1) — unchanged from v1

*See v1 — §4. Keep as-is. `wait_for_stable()` called before every observe
and every extract. Check: network-idle + DOM-mutation-idle + no loading
indicators.*

### Speed move

Parallel subtask for "is page still loading?" check — check network + DOM
+ loaders in parallel, `asyncio.gather` on all three. Saves ~200ms vs
serial per observe.

---

## 9. Expectation-vs-reality verify + dual grounding (Gemini #2 + #3) — unchanged

*See v1 §5 + §6. Every `act` node outputs `verification_text` +
`expected_post_state`; verify node enforces both.*

### Speed move

Use Haiku for the "does snapshot match expected state?" call (tiny prompt,
yes/no answer). Reserve Sonnet for navigator, Opus for planner + judge.

---

## 10. Specialist sub-agents (Gemini #4) — updated

v1 kept. Update: platforms are loaded from `specialists/<platform>/` when
Stage B classifier returns high confidence AND canonical action test
passes. Demotion: on 2 consecutive step failures, demote to generic +
profile path for remainder of sample. Re-promote on next sample.

---

## 11. Deterministic extractors (Gemini #5) — unchanged

*See v1 §8. Task schema declares `extraction: {locate, parse, validators}`.
LLM locates DOM node, code parses + validates.*

### Speed move

Batch extraction within a sample: identify ALL target fields from the
current snapshot in one LLM call, not one-per-field.

---

## 12. Failure recovery matrix (consolidated)

| Failure source | Detection | Recovery | Data integrity |
|---|---|---|---|
| Stale profile (site changed) | `dom_schema_hash` mismatch | Pause group; rebuild profile via Stage D-F | No wrong data; pause is visible |
| Auth session died | `auth_check` returns login_wall | Refresh (programmatic) or alert (interactive) | Sample checkpointed; resume after refresh |
| Site rate-limited us | 429 / challenge page | AIMD multiplicative decrease | Sample retried after bucket refill |
| Proxy burned (fingerprinted) | Elevated failure rate on IP | Circuit-break that IP; cool-down | Pool replacement; no data loss |
| Pod crashed | Lease TTL expiry | Warm standby acquires lease + proxy_id | LangGraph checkpoint replay |
| Chromium OOM | Process exit | New Chromium same state + proxy | Context samples replay |
| Redis unavailable | Connection error | Cluster-wide pause; resume on recovery | No new decisions; in-flight checkpointed |
| Proxy vendor outage | Pool-wide failure rate | Drain mode; alert; block new runs | In-flight sent to park queue |
| Self-test fails on new platform | Stage F gate | Teach mode OR fail run with message | Zero samples executed with bad profile |
| Canary batch <60% pass | Stage G gate | Dashboard review | 995 samples never dequeued |
| Extract schema validation fails mid-run (>5% window) | Rolling monitor | Pause group; rebuild profile | Failed samples flagged for review, not silently written |

---

## 13. Speed/accuracy tradeoff matrix (explicit decisions)

Every speed optimization across this doc, with the accuracy cost we accept
and the guard that caps it.

| Optimization | Speedup | Accuracy cost | Guard |
|---|---|---|---|
| Parallel probe + exploration at run-start | ~Nx (N = unique hosts) | None if probe uses rendered Chromium | Probe verdict hashed; wrong verdict invalidates Stage B |
| Cached platform profiles | 90s/run saved | Stale profile = wrong extractions | DOM-schema-hash proactive invalidation + 5% schema-fail monitor |
| Skip canary on 7-day-clean profile | 5 samples × 15s = 75s | Undetected drift | Any prior-run failure forces canary |
| AIMD on rate limit (vs conservative fixed) | 5-10x on underutilized hosts | First-minute 429s | Decrease on any limit signal; bucket per endpoint-class |
| IP pre-warm on challenge sites | ~3s/sample saved on Q2 | Warm cookies expire | Re-warm every 30 min |
| Haiku for auth_check + verify | 3x cheaper/faster than Sonnet | Miss subtle failures | Short-circuit on explicit error markers; elevate to Sonnet on ambiguous |
| Batch field extraction | 5 LLM calls → 1 | Context dilution | Still apply jsonschema + deterministic parsers per field |
| Memoized auth_check by (url, dom_hash) | 60% fewer calls | Cached "authed" on expired session | Cache TTL 60s; invalidate on any POST |
| Warm-pool pods | Zero failover delay | Lease race | Fence tokens prevent split-brain |
| Specialist fast-path (skip tier 2) | 90s/host | Wrong specialist | Canonical action test must pass before commit |
| Parallel contexts on one auth | ~10x | Site per-session cap | Cap respects `per_session_concurrency` in creds config |

**Where we DO NOT trade:**
- Profile self-test (Stage F): always runs before caching.
- Canary batch (Stage G): always runs on new profile unless explicitly overridden.
- Schema validation at extract: always runs; never skipped for speed.
- Hash-chain audit log append: always synchronous; no async write.
- `wait_for_stable` before observe: always blocking.

---

## 14. Migration order

Updated to reflect v2. Each step shippable, none regresses eval.

1. **Stabilization (§8).** Half day. Immediate accuracy lift.
2. **Verification_text + dual grounding (§9).** One day. Biggest misclick fix.
3. **Deterministic extractors (§11).** One day. Schema-driven parsing.
4. **Auth vault v2: per-URL surfaces + sealed state + probe (§2.1–2.3).** One day.
5. **Auth-check node + refresh strategies (§2.4).** Half day.
6. **Redis AIMD rate limiter (§3).** Half day.
7. **Proxy pool v2: 4-quadrant policy + circuit breaker (§4).** One day.
8. **Scheduler v2: auth-group + proxy-held lease + fair queueing (§5).** One day.
9. **Stages A-H for unknown platforms (§7).** Two days. Biggest single feature.
   - A-C ship first (probe, classify, auth gating).
   - D-E next (task-guided exploration + profile synthesis).
   - F-G next (self-test gate + canary). **Do not ship D-E without F-G.**
   - H hooks into the existing full-run path.
10. **Teach mode (§7.6).** One day. UI-heavy; ship last.
11. **Multi-host sample support (§6).** Half day. Gate on real need.

Steps 1-3 alone → ~95% accuracy on the 5 trial tasks.
Steps 4-8 → production-ready at 1000-sample scale with auth + rate limits.
Step 9 → unknown-platform coverage.
Step 10 → long-tail opaque platforms.

---

## 15. Invariants (unchanged across v1→v2)

- Hexagonal contracts; every new capability is a Protocol.
- One switch panel: `profile.yaml` + `credentials.yaml`.
- Hash-chained audit log for every decision, every action, every refresh.
- Content-addressed artifacts; screenshots + profiles + traces alike.
- Bounded reflection: N=3 per sample; auth refresh counts against budget.
- **No wrong data ships silently.** Every accuracy-critical path has either
  a validation gate, a canary, a cross-check, or a human escalation.

Accuracy comes first. Speed is what we do once accuracy is guaranteed.
