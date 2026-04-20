# Problems & Solutions — Case by Case

*Scannable reference. Every problem is split into concrete cases. For each
case: how we detect it, how we solve it. No code — just decision rules,
tables, and diagrams.*

---

# Problem 1: Authentication

Real sites have eight different auth flavors. One strategy fails on seven
of them. We handle each case explicitly.

## Decision flow at run-start

```
  New host encountered
         │
         ▼
   Probe with real browser
         │
   ┌─────┴─────────────────────────────────────────┐
   │                                               │
   ▼                                               ▼
  Content visible?                         Login wall detected?
   │                                               │
   ▼                                               ▼
  Case 1.1 (no auth)                     What kind of login?
                                                   │
                ┌─────┬─────┬─────┬─────┬─────┬────┴──┬─────┐
                ▼     ▼     ▼     ▼     ▼     ▼       ▼     ▼
              1.2   1.3   1.4   1.5   1.6   1.7     1.8   1.9
              PAT  Pass  OAuth SSO  TOTP  Push   CAPTCHA Device
```

---

## Case 1.1 — No auth required

**Examples:** public docs, blogs, open data portals, GitHub public repos.

**How we detect:**
- Probe returns rendered content matching expected structure
- No login form in DOM
- No redirect to `/login` or external IdP
- No 401/403 on the target URL

**How we solve:**
- Mark host as `auth=none` in Redis cache
- Skip all credential loading
- Free to use rotating IPs (no session to preserve)

---

## Case 1.2 — Personal Access Token / API key

**Examples:** GitHub PAT, Linear API key, most developer-facing platforms.

**How we detect:**
- Declared by operator in credentials config
- Probe with token present → content visible
- Probe without token → login wall

**How we solve:**
- Operator stores token in `.env` as a secret reference
- Agent injects token as header or cookie on every context
- No browser-level login flow needed
- IP rotation is safe (tokens are IP-independent by design)
- **Throughput advantage:** this is the fastest-to-automate auth case

---

## Case 1.3 — Username + password form

**Examples:** most SaaS without SSO, legacy admin panels, internal tools.

**How we detect:**
- Probe returns a page with `<input type=password>`
- Form action posts to same or adjacent URL
- No IdP redirect

**How we solve:**
- First time: operator runs `andera login` → real Chromium opens → they fill form → we save the resulting session as a sealed encrypted blob keyed by host
- Next runs: blob loaded into every context for this host
- When session dies: if operator stored password in vault → agent re-logs-in programmatically; otherwise alert operator

---

## Case 1.4 — OAuth redirect

**Examples:** "Sign in with Google", "Sign in with GitHub" flows.

**How we detect:**
- Login page has "Sign in with X" button or auto-redirects to `accounts.google.com` / similar
- Post-login returns with an authorization code in URL

**How we solve:**
- Operator runs `andera login` once → completes full OAuth dance interactively including any consent screens → we capture the post-redirect session
- Subsequent runs load sealed blob (same as Case 1.3)
- OAuth tokens have their own TTL; refresh by programmatic re-auth if refresh token is present, else re-run `andera login`

---

## Case 1.5 — SSO / SAML

**Examples:** enterprise Workday, Salesforce, Microsoft 365 with Okta IdP.

**How we detect:**
- Initial visit redirects to `idp.myco.com` or similar identity provider
- SAML assertion in the response payload
- Final redirect back to the app with a session cookie

**How we solve:**
- Same interactive login flow as Case 1.4 — we capture the full redirect chain's end state
- **Key risk:** SAML assertions sometimes bind to IP. If the site is strict, the session dies when IP changes → treat as Case 2.4 strict-auth scenario (sticky IP mandatory)

---

## Case 1.6 — Time-based One-Time Password (TOTP / Google Authenticator)

**Examples:** GitHub with 2FA, AWS console, most banks.

**How we detect:**
- Login succeeds at password step → redirected to 6-digit code page
- Page has `<input maxlength=6>` or mentions "authenticator app"

**How we solve:**
- Operator provides the TOTP seed (the secret behind the QR code) during initial setup
- Stored encrypted; agent generates codes on demand using standard TOTP algorithm
- Fully automatable after one-time setup — no human needed for refreshes
- **Preferred** over push MFA whenever the site offers both

---

## Case 1.7 — Push MFA (Duo, Okta push, Microsoft Authenticator)

**Examples:** many enterprise Okta deployments, some banks.

**How we detect:**
- Login redirects to a page saying "Approve on your phone"
- No code input field, just a waiting spinner

**How we solve:**
- **Cannot fully automate.** Flag host as `requires_human_mfa` in config
- At run start: system alerts operator "Please approve the MFA prompt for host X within 60 seconds"
- Operator taps phone → session captured → runs proceed
- During run: if session dies and needs refresh, samples for this host park; operator re-authorizes; samples resume
- **Workflow design:** batch samples per host so one MFA approval unlocks a large run

---

## Case 1.8 — CAPTCHA (recurring, not just at signup)

**Examples:** LinkedIn under suspicious-activity detection, banks at random intervals.

**How we detect:**
- Page contains reCAPTCHA iframe or hCaptcha widget
- Text like "Verify you are human" in main content

**How we solve:**
- **Do not attempt automated solving** (brittle, bans the IP on failure)
- Treat each CAPTCHA as an auth interruption — park sample, alert operator
- **Prevention is better:** minimize CAPTCHA triggers by
  - Using residential proxies (lower CAPTCHA rate than datacenter)
  - Keeping auth session sticky on one IP (CAPTCHA often triggers on IP change)
  - Respecting rate limits aggressively (burst = CAPTCHA)

---

## Case 1.9 — Device trust / fingerprint binding

**Examples:** Google Workspace, Microsoft 365, Chase/Wells Fargo online banking.

**How we detect:**
- After password login, site asks "Is this a trusted device?" or sends email code
- Session silently breaks when loaded on a different Chromium instance

**How we solve:**
- Capture the full browser profile during `andera login`, not just cookies — includes local storage, IndexedDB, service worker state
- Use the **same Chromium version** across all agent containers (Playwright-pinned)
- Use the **same user-agent and viewport** in every context loading this profile
- For sites that are strictly device-pinned: run ALL samples for this host on the pod that owns the session blob; do not share across pods

---

## Case 1.10 — Mixed surfaces on one host

**Examples:** GitHub (public repos = no auth, private = PAT), Jira (public issues = none, project issues = auth).

**How we detect:**
- Different URL patterns on same host have different probe results
- Some 200-OK, some 401, some redirect

**How we solve:**
- Treat auth as **per-URL-pattern**, not per-host
- Credentials config lists multiple "surfaces" per host, each with its own rule
- Each request picks the strategy for its URL pattern at dispatch time

---

## Case 1.11 — Session died mid-run (any strategy)

**How we detect:**
- Post-action snapshot shows login wall
- HTTP 401 on a request that worked earlier
- Redirect to login URL

**How we solve (per original auth strategy):**

| Original case | Recovery |
|---|---|
| 1.2 PAT | Re-read from vault (token rotation); if still fails, alert operator |
| 1.3 Password | Programmatic re-login using stored creds |
| 1.4 OAuth | Refresh token if available, else alert |
| 1.5 SSO | Often requires re-login; alert if auto-fail |
| 1.6 TOTP | Programmatic with stored seed — fully automatic |
| 1.7 Push MFA | Park samples, alert operator |
| 1.8 CAPTCHA | Park samples, alert operator |
| 1.9 Device trust | Alert — may need full re-setup |

---

# Problem 2: Speed of processing all samples

**The core tension:** more parallelism is faster, but sessions and rate
limits and site policies constrain how much parallelism is safe. The
right parallelism strategy depends on which quadrant the target sits in.

## Four-quadrant model

|                    | Simple site                  | Challenge-first site (Cloudflare/Akamai) |
|--------------------|------------------------------|------------------------------------------|
| **No auth**        | Q1 — fast fan-out            | Q2 — warm pool                           |
| **Auth required**  | Q3 — sticky IP, many contexts | Q4 — sticky IP, few contexts              |

Every host gets classified into one quadrant at probe time. Parallelism
strategy follows.

---

## Case 2.1 — Q1: No auth, simple site

**Examples:** public data portals, blog aggregators, open-data APIs.

**How we detect:**
- Probe returns content immediately (no challenge page, no login wall)
- No Cloudflare/Akamai headers in response

**How we solve:**
- Maximum parallelism across pods
- Each context gets a **fresh rotating IP** per sample
- Pods × contexts × per-context-rps ≤ site aggregate RPS (discovered via AIMD)
- Wall time for 1000 samples at 10s each, 50 contexts across pods: **~3 minutes**

---

## Case 2.2 — Q2: No auth, challenge-first site

**Examples:** LinkedIn public pages, news sites, e-commerce catalogs, most sites behind Cloudflare.

**How we detect:**
- Probe page shows a JS challenge (5-second "Checking your browser")
- Response headers contain `cf-ray`, `server: cloudflare`, or similar
- First request from a new IP is slow; subsequent are fast

**How we solve:**
- **Warm pool** of N stable IPs (say 10-20 per host)
- At run start: each IP visits the home page once to clear the challenge; cookies cached
- Samples then use one of these warm IPs
- Do **NOT** rotate per sample — every rotation pays the 2-5 second challenge cost
- Rotate only when an IP shows signs of being burned (elevated failure rate)
- Wall time for 1000 samples, 15 warm IPs, 10s/sample: **~12 minutes**

---

## Case 2.3 — Q3: Auth, lenient site

**Examples:** GitHub with PAT, Linear, Jira, most developer tools.

**How we detect:**
- Auth succeeds
- Session survives being used from multiple IPs within a short window
- No "unusual activity" email or force-logout

**How we solve:**
- One sticky IP per session, but session can handle ~10-30 parallel contexts
- Pod holds the auth blob and the proxy lease
- Parallelism within a pod = number of contexts (bounded by site's per-session cap + pod RAM)
- Multiple pods = multiple (account, host) pairs for this host — fully parallel
- Wall time for 1000 samples, 1 account, 20 contexts: **~8 minutes**

---

## Case 2.4 — Q4: Auth, strict site

**Examples:** Workday, Salesforce, LinkedIn authenticated, bank portals.

**How we detect:**
- Sessions die when used from 2+ IPs
- "Login from another location detected" emails after fan-out
- Site enforces hard per-session concurrency (often 3-10)

**How we solve:**
- One sticky IP per session, held by **the proxy lease in Redis**, not by the pod
- Pod holding the lease runs N contexts (N = site's per-session cap, typically 5)
- If pod crashes, another pod takes over the SAME proxy id and SAME session blob — site sees no IP change
- **Hard ceiling:** 1000 samples ÷ 5 concurrent × 15s/sample = **~50 minutes minimum**. Cannot go faster without a second account.

---

## Case 2.5 — One user account, 1000 samples, strict site

**The ceiling problem.** User gives us one Workday account and says "scale to 1000."

**How we detect:**
- Credentials config lists exactly one credential for the host
- Host is Q4

**How we solve (honest):**
- Explain the ceiling: wall time = `samples × time_per_sample / per_session_cap`
- Offer throughput options, priced by effort:

| Option | Wall time | Cost |
|---|---|---|
| One account, 5 parallel | 50 min | Free |
| Two accounts provided | 25 min | User creates 2nd account |
| Five accounts provided | 10 min | User creates 5 accounts |
| Negotiate with site for API access | 2 min | Weeks of vendor conversation |

- Default: run at the ceiling; surface the estimate in the dashboard at run start so the user can decide.

---

## Case 2.6 — Multi-host samples

**Example:** "Read Linear ticket → search GitHub for related PRs → post summary to Slack."

**How we detect:**
- Sample's plan touches more than one host

**How we solve:**
- Sample's **primary host** = first host in the plan; owns the pod lease for the sample's lifetime
- **Secondary hosts** acquired as sub-leases when plan reaches them
- Each host's auth + proxy independent; one pod opens multiple contexts
- Accuracy guard: auth check runs independently per host

---

## Case 2.7 — Pod crashes mid-run

**How we detect:**
- Lease heartbeat expires (no heartbeat for 30 seconds)
- Redis detects missing pod

**How we solve:**
- Another pod acquires the orphaned lease
- **Same proxy id** (held by the Redis lease, not the pod) → target site sees continuous IP
- **Same session blob** (loaded from content-addressed store)
- Samples resume from their last LangGraph checkpoint on the new pod
- No session death, no duplicate processing

---

## Case 2.8 — Site rate-limits us mid-run

**How we detect:**
- HTTP 429 response
- Cloudflare challenge appears on a previously-uncontested URL
- "Slow down" banner in HTML
- Abnormal latency spike

**How we solve:**
- AIMD controller halves the current rate limit immediately
- Requests queue in Redis bucket until tokens available
- Over next minute of success, rate slowly ramps back up (by 25%)
- Self-tuning; no manual threshold configuration needed

---

## Case 2.9 — Mixed quadrants in one run

**Example:** 500 Workday samples (Q4) + 400 GitHub samples (Q3) + 100 public-data samples (Q1).

**How we detect:**
- Samples declare (or plan reveals) different target hosts
- Each host's quadrant already known from probe

**How we solve:**
- Scheduler groups samples by (host, credential) into **auth groups**
- Each group has its own sub-queue and parallelism budget
- Groups run fully in parallel — GitHub doesn't wait for Workday
- Wall time = MAX of per-group wall times, not sum

---

## Case 2.10 — Large group starves small group

**Example:** 990 Workday samples + 10 Linear samples. Linear users wait for Workday to finish before their 10 samples start.

**How we detect:**
- Redis queue depth imbalance across groups
- Group lease acquisition heavily favoring large group

**How we solve:**
- **Weighted fair queueing** on lease acquisition
- Each group gets pod-time proportional to its queue depth, with a minimum floor
- Small groups finish in reasonable wall time even when large ones are running

---

# Problem 3: Platform is unknown

**The accuracy killer.** Known platforms (GitHub, Linear, Workday) work
well because the LLM has training-data priors. Unknown platforms (custom
ERPs, regional HRIS, vendor portals) break everything.

## Fallback ladder

```
  Sample on a new host
         │
         ▼
  Tier 1: Known-platform fast path
         │   (specialist prompt; tested)
         │
         ▼ fails or not-known
  Tier 2: Generic + platform profile
         │   (task-guided exploration builds profile)
         │
         ▼ fails self-test
  Tier 3: Visual-first fallback
         │   (screenshot grounding; SoM; less DOM reliance)
         │
         ▼ fails canary
  Tier 4: Teach mode
         │   (human demonstrates sample 1; we abstract)
         │
         ▼ fails
  Tier 5: Human escalation
             (can't automate; flagged in dashboard)
```

No sample runs on tier N+1 until tier N has been tried and its gate has
failed. No sample runs on 1000-sample scale until the chosen tier has
passed self-test AND canary.

---

## Case 3.1 — Known platform, high confidence

**Examples:** GitHub, Linear, Jira, Workday, Salesforce (classic).

**How we detect:**
- Domain matches known list
- DOM fingerprint matches expected
- Canonical action test passes (e.g., "open profile menu" succeeds)

**How we solve:**
- Load platform's specialist prompt bundle
- Planner prompt includes platform-specific pitfalls ("in Jira, the assignee dropdown is lazy-loaded")
- Extractor uses platform-specific selector hints
- **Fastest path**, ~95% accuracy out of the box

---

## Case 3.2 — Known-platform classifier wrong

**Example:** site looks like GitHub (same headers, same visual) but is actually GitHub Enterprise with custom SSO and different URL shapes.

**How we detect:**
- Classifier initially says "GitHub, confidence 0.95"
- Canonical action test fails (can't find expected profile menu)
- First specialist-plan step returns unexpected response

**How we solve:**
- **Demote** to Tier 2 (generic + explore)
- Build platform profile as if unknown
- Re-classify as `github_enterprise_variant` for future runs on this host
- **Never silently retry** with the wrong specialist — that compounds errors

---

## Case 3.3 — Unknown but similar to known

**Example:** A vendor portal that looks SPA-ish, has nav + list + detail pattern.

**How we detect:**
- Classifier returns `unknown` or low confidence
- DOM fingerprint matches generic CRUD pattern

**How we solve:**
- **Task-guided exploration:** feed NL task + first input row to planner, ask "which nav items match this intent?"
- Visit top 3 matches; snapshot each
- Synthesize a platform profile with **motor programs**:
  - `to_reach_detail_view` → click rows, match text against input
  - `to_paginate` → scroll or click next
  - `to_filter` → type in search box
- Run mandatory **self-test** on sample 0; validate schema
- If passes: run **canary batch** of 5 samples; unlock 995 only if all 5 pass

---

## Case 3.4 — Unknown, DOM is hostile (heavy canvas, images, iframes)

**Example:** Tableau dashboards, embedded analytics, old Flash replacements, some PDF viewers.

**How we detect:**
- DOM snapshot is mostly `<canvas>`, `<iframe>`, or `<img>`
- Inner text is sparse or missing
- Interactive elements are drawn not marked up
- ARIA labels absent

**How we solve — visual-first fallback (Tier 3):**
- Set-of-Mark overlay becomes primary input (bounding boxes drawn on screenshot)
- Planner gets the screenshot as main grounding; DOM as secondary
- Extractor reads pixels via multimodal LLM when DOM can't provide text
- Slower (larger prompts, more vision tokens) but catches what text-only can't
- **Accuracy guard:** screenshot-extracted values get SECOND-pass validation — LLM must cite the pixel region it read from; judge verifies region contains the claimed text

---

## Case 3.5 — Unknown, exploration can't figure it out

**Example:** A niche platform with non-obvious nav (buried in kebab menu), custom UX conventions.

**How we detect:**
- Profile self-test fails on sample 0
- Canary batch <60% pass

**How we solve — teach mode (Tier 4):**
- Dashboard tells operator: "We can't plan this platform automatically. Please walk through sample 1."
- Non-headless Chromium opens; operator drives
- We record every click, type, navigation as a trace
- LLM **abstracts** the trace: literal values from the operator's sample become parameters keyed to input fields
- Replay on sample 2 with different input data; validate schema match
- If sample 2 matches: abstracted trace becomes the "plan template" for this platform
- Only then do remaining samples run

---

## Case 3.6 — Unknown, teach mode replay fails

**Example:** The abstracted trace worked on sample 1 but sample 2 has different data that takes a different path through the site.

**How we detect:**
- Sample 2 replay diverges from the trace
- Schema validation fails on sample 2

**How we solve — human escalation (Tier 5):**
- Mark the platform as `requires_manual` in config
- Dashboard shows: "Each sample on this platform needs human review"
- Operator decides: skip these samples, manually handle them, or invest in better automation for this platform later
- **Audit log records the escalation decision** so reviewers know why these rows are missing

---

## Case 3.7 — Profile cached, but site changed overnight

**Example:** Run yesterday worked; same run today returns empty extractions.

**How we detect:**
- DOM fingerprint on page snapshot ≠ cached fingerprint in profile
- Computed before sample runs, not after failure

**How we solve:**
- **Pause** the auth group immediately
- Rebuild profile: re-run Tier 2 exploration + self-test + canary
- Resume only when new profile passes all gates
- Flag any samples that ran between yesterday's success and today's detection as "requires review" — data integrity preserved, not silently trusted

---

## Case 3.8 — Same host, multiple UI versions

**Example:** Workday classic vs Workday lightning on same domain; Salesforce classic vs LEX.

**How we detect:**
- UI fingerprint at session start (presence of specific CSS class or meta tag)
- Differs between contexts on same host

**How we solve:**
- Profile keyed by `(host, ui_version)` not just host
- Two profiles can coexist for one domain
- Each context's profile lookup uses its detected UI version

---

## Case 3.9 — Task needs reasoning on non-text content

**Example:** "What's the peak in this chart?" "Which region has the highest sales on this map?"

**How we detect:**
- Task schema has fields whose values aren't findable in DOM text
- Page contains charts, graphs, maps as primary content

**How we solve — visual extraction:**
- Extractor runs multimodal LLM on the screenshot
- LLM must return: value + bounding-box region it read the value from
- Judge validates: does the region actually contain that value? (secondary LLM call with cropped image)
- Add `extraction_method=visual` to audit log for the field — reviewer can see this was pixel-based

---

## Case 3.10 — Multiple unknown hosts in one run

**Example:** 10 different customer-specific portals, one-off each, 100 samples each.

**How we detect:**
- Run enumerates unique hosts; several are marked unknown
- Each needs Tier 2 pipeline before samples can start

**How we solve:**
- **Run probe + exploration in parallel** for all unique hosts at run-start
- 10 hosts × 60s exploration = 1 minute of wall time (not 10)
- Each host independently gates on its own self-test + canary
- Host that passes fast unblocks its 100 samples; slow host's 100 samples wait for its own gate

---

## Case 3.11 — Same platform seen before, new client setup

**Example:** Workday at company A (with customizations) vs Workday at company B (different customizations).

**How we detect:**
- Domain differs but platform family recognized
- DOM fingerprint partially matches cached Workday profile

**How we solve:**
- Load base Workday specialist bundle
- Run a **lightweight re-exploration** (just the task-relevant pages, 15 seconds)
- Overlay client-specific findings on the base profile
- Self-test + canary still mandatory — customizations can break specialist assumptions

---

## How tiers relate (summary)

| Tier | Used when | Accuracy | Speed | Cost |
|---|---|---|---|---|
| 1 — Known fast path | Classifier + action test pass | Highest | Fastest | Cheapest |
| 2 — Profile from exploration | Unknown, DOM-readable | High if gates pass | Moderate | Moderate |
| 3 — Visual fallback | DOM hostile | Moderate-high | Slower | Higher (vision tokens) |
| 4 — Teach mode | Profile self-test fails | High if replay transfers | Slow (human in loop) | Highest per-sample |
| 5 — Human escalation | All else fails | Guaranteed (human does it) | Slowest | Manual labor |

**Accuracy floor:** at every tier, the gates (self-test + canary + DOM
fingerprint) mean wrong data never ships silently. If we can't hit the
accuracy bar at the current tier, we demote. If we can't hit it at Tier
5, the work doesn't ship at all — the dashboard shows "requires manual."

---

# Problem 1 × Problem 2 × Problem 3 — how they combine

Real runs hit all three. Example scenarios:

## Scenario A — 1000 samples, GitHub public repos

- Problem 1: **Case 1.1** (no auth)
- Problem 2: **Case 2.1** (Q1, fan out)
- Problem 3: **Case 3.1** (known platform, fast path)

Wall time: ~3 minutes. Accuracy: ~95% with specialist bundle.

## Scenario B — 1000 samples, one Workday account

- Problem 1: **Case 1.5** (SSO) + potential **1.6** (TOTP) or **1.7** (push)
- Problem 2: **Case 2.4** (Q4 strict, hard ceiling)
- Problem 3: **Case 3.1** (known) OR **3.8** (UI version differs) OR **3.2** (enterprise variant)

Wall time: 30-50 minutes depending on UI version. Accuracy: ~95% on
Workday classic, gated rebuild required for Workday custom instances.

## Scenario C — 1000 samples, unknown customer portal with password auth

- Problem 1: **Case 1.3** (password form)
- Problem 2: **Case 2.3** (Q3 if lenient) or **Case 2.4** (Q4 if strict) — discovered at probe
- Problem 3: **Case 3.3** (unknown, exploration) → gates → canary → run

Wall time: 90s exploration + canary (~75s) + full run at ceiling. 1000
samples at 15s and 10 contexts: total ~30 minutes.

## Scenario D — 1000 samples, Tableau dashboard

- Problem 1: **Case 1.3** or **1.5** depending on customer
- Problem 2: **Case 2.3** most often
- Problem 3: **Case 3.4** (visual-first fallback — DOM is canvas)

Wall time: ~2x normal due to vision-token cost per sample; accuracy
preserved by visual-extraction validation.

## Scenario E — 1000 samples, requires push MFA

- Problem 1: **Case 1.7** (push)
- Problem 2: any quadrant
- Problem 3: any tier

Flow: operator approves MFA at run start → run proceeds → if session
dies mid-run, samples park → operator re-approves → resume. Wall time
depends on MFA refresh frequency.

---

# The non-negotiables

No matter which combination of cases a run hits, these are always true:

- **Self-test gates every new profile.** No untested plan runs on bulk samples.
- **Canary batch gates every fresh run.** First 5 samples always reviewed.
- **DOM fingerprint is checked before every sample.** Stale profile → pause.
- **Audit chain records every decision.** Reviewer can trace any value to its source.
- **Wrong data never ships silently.** If we can't verify, we flag. If we can't flag, we fail.
- **Speed is second to accuracy.** Every optimization has a guard, or we don't take it.

The system is designed so that failures surface. Success is the absence of
flagged rows in the dashboard. That's what audit-grade means in practice.
