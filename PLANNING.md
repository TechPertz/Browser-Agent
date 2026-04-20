# Planning — what actually happens, step by step

*Ground-up walkthrough. Starts the moment a URL arrives at a pod and
walks all the way through to a row written to output.csv. Explains what
gets pulled from the page, what gets sent to an LLM vs done
deterministically, when we fall back to visual or teach-and-learn, and
how we handle pages that are too long to send whole.*

*No code. Concrete examples. If you read only this doc you should
understand how planning + extraction actually work.*

---

## Step 0 — What arrives at the pod

The agent never sees a raw task from a human. It sees a sample object
pulled off a Redis queue, carrying:

| Field | Example value | Source |
|---|---|---|
| `run_id` | `run-abc123` | assigned when operator submitted the run |
| `sample_id` | `run-abc123-00017` | one per input row |
| `task_prompt` | "For each Linear ticket URL, extract the ticket number and assignee." | the task YAML |
| `input_data` | `{url: "https://linear.app/a/issue/LIN-1"}` | one row from the input CSV |
| `start_url` | `"https://linear.app/a/issue/LIN-1"` | derived from input_data |
| `extract_schema` | `{type: object, required: [ticket_no, assignee], ...}` | the task YAML |
| `host` | `linear.app` | parsed from start_url |
| `auth_group` | `linear.app + token-1` | matched from credentials config |

Before this sample arrives, the pod already acquired the right auth
lease and bound to the right proxy IP. That happened in infrastructure,
not in the agent.

---

## Step 1 — Browser context opens

The pod opens a fresh Chromium context with three things attached:

1. **Proxy** — pinned to this auth group's IP.
2. **Storage state** — if the host requires auth, the sealed encrypted
   session blob gets loaded (cookies, localStorage, IndexedDB).
3. **User agent + viewport** — fixed across all contexts of this host
   to avoid fingerprint mismatch.

The context is private to this sample. Samples share a Chromium process
to save memory, not cookies.

---

## Step 2 — The preflight goto

Before any LangGraph node runs, the agent does one thing: navigate to
`start_url`.

Why before planning? Because for unknown platforms, planning without
seeing the page is guaranteed to hallucinate. Even for known platforms,
we want the recognize step to confirm "yes, this really is what we
think it is" by looking at an actual first page.

After the goto, we **wait for the page to stabilize**. This is
critical. The load event fires as soon as the browser thinks the page
is done, which is usually before anything real is there. We wait for
three things to all be quiet for 500 milliseconds:

- No pending network requests (excluding long-lived websockets).
- No DOM mutations.
- No visible loading indicators on the page.

Only when all three are quiet do we proceed. This adds ~500-1500ms but
prevents the single biggest class of failure: acting on a half-rendered
page.

---

## Step 3 — The first snapshot: what we actually pull from the page

This is the heart of how the agent understands a page. Not raw HTML,
not a full DOM dump, not a naive screenshot. We extract **five pieces**,
each designed for LLM consumption.

### 3.1 URL and title

Trivial. What page we're on.

### 3.2 `inner_text`

The visible text content of the page body. How we get it:

- Playwright evaluates JavaScript in the page to walk the DOM.
- For each text node: check if its nearest container is visible
  (non-zero bounding box, not `display: none`, not `visibility: hidden`).
- Skip text inside `<script>`, `<style>`, `<noscript>`.
- Collapse runs of whitespace to single spaces.
- Join in reading order.

Result: a string. For a typical page, 1-20 KB. For a very long page
(like a 200-comment GitHub issue), up to ~100 KB. We deal with that
later.

### 3.3 `outline`

A compact structural sketch. Think of it like a table of contents. We
extract:

- Every `<h1>`, `<h2>`, `<h3>`, `<h4>` with its text.
- Every ARIA landmark (`role=navigation`, `role=main`, `role=complementary`).
- Optionally: section boundaries derived from CSS or semantic tags.

Result: an indented list of a few hundred characters that tells the
LLM the shape of the page without dumping every `<div>`.

Example for a Linear ticket page:

```
[nav] Workspace / Navigation
[h1]  LIN-1 — Implement auth flow
[h2]  Description
[h2]  Activity
[h3]  Comments (3)
[h2]  Properties
```

### 3.4 `interactive`

**This is how the agent knows what's clickable.** A pre-computed list
of every interactive element on the page.

How we build this list (all deterministic, no LLM):

1. **ARIA role check.** Any element with an interactive role:
   `button`, `link`, `textbox`, `combobox`, `menuitem`, `tab`,
   `checkbox`, `radio`, `switch`, `option`.
2. **HTML tag check.** Any `<button>`, `<a href>`, `<input>`,
   `<select>`, `<textarea>`, `<summary>`.
3. **Event handler check.** Elements with `onclick`, `onkeydown`, or
   `role` added via JavaScript.
4. **Keyboard reachable.** Elements with `tabindex >= 0`.
5. **Visual hint.** Elements where computed `cursor: pointer`.

Any element passing one of these goes into the list. For each entry
we record:

- **Role** — what kind of thing it is (`link`, `button`, …).
- **Accessible name** — from `aria-label`, `aria-labelledby`, visible
  inner text, `alt` attribute, or `title`, in that order of preference.
- **Selector** — a stable way to find this element later (role-based
  selector preferred, CSS selector as backup, text-match as last
  resort).
- **Tag** — the underlying HTML tag.
- **Index** — position in reading order.

Example entries:

- `{role: link, name: "Pull Requests", selector: "role=link[name='Pull Requests']", tag: a}`
- `{role: button, name: "Submit", selector: "button#submit", tag: button}`
- `{role: textbox, name: "Search", selector: "input[type=search]", tag: input}`

Typical count: 20-200 interactive elements per page. We keep them all
(it's cheap — just labels).

### 3.5 `page_state`

Readiness flags the planner uses to sanity-check:

- `network_idle`: true/false.
- `dom_idle`: true/false.
- `loading_indicators_present`: list of visible spinner selectors, if any.
- `has_error_banner`: true/false.

---

## Step 4 — How the system separates "data" from "clickable"

This is the question you asked. Here is the complete answer.

**Clickable = whatever's in the `interactive` list.** We built that
list deterministically in Step 3.4 using five independent signals. An
LLM never has to guess "is this a button?" — the representation
answers that question before any prompt is built.

**Data = everything in `inner_text` that is not inside an interactive
element.** The text you'd read on the page.

When the planner or extractor needs to pick a value, it consumes these
as two separate inputs:

- `interactive` → "here are the things you can act on"
- `inner_text` → "here is the content on the page"

Example. The task says "find the assignee of this ticket." The page
has a sidebar labeled "Assignee: Jane Smith" and a button that says
"Change Assignee." Both mention "assignee." Which one is the value?

- The button is in `interactive` with role=button and name="Change Assignee."
- The label + value pair lives in `inner_text` with no interactive role.

The extractor sees both, but because of the roles, it knows the
interactive one is a CONTROL (clicking changes the assignee) and the
non-interactive one is the VALUE (the current assignee). It reads
"Jane Smith" from inner_text.

This is why the agent doesn't misclick or misextract on pages that
have both labels and buttons using the same words. The separation is
done in the snapshot, not in the LLM's head.

---

## Step 5 — Classify: what SHAPE of task is this?

Now the agent has seen the page once. The first LangGraph node to fire
is `classify`. It does NOT look at the page. It looks at the task.

**What it gets:**

- The natural-language task prompt.
- The extract schema.

**What it does:**

- Sends a tiny Haiku call with a system prompt like "You are a task
  classifier. Return one of: extract, form_fill, list_iter, navigate,
  generic."
- Haiku returns one token. We parse it.

**Why classify without the page?** The task shape — "extract fields
from one page" vs "iterate a list" vs "fill a form" — is determined
by the task description and schema, not the page. This lets us cache
the classification: every sample of the same task gets the same
task_type for free.

**Output:** `task_type = "extract"` (or whichever).

---

## Step 6 — Recognize: what PLATFORM is this?

Second node. THIS one does look at the page (the preflight snapshot).

**What it gets:**

- The host (e.g., `linear.app`).
- The first snapshot (URL, title, outline, interactive count,
  inner_text length).
- A DOM fingerprint (hash of stable structural markers like tag
  sequence + ARIA role counts).

**What it does — three signals in parallel:**

1. **Domain match.** Is the host in our known list (github.com,
   linear.app, workday.*, etc.)?
2. **Fingerprint match.** Does this DOM fingerprint appear in our
   cache of known platforms?
3. **Canonical action test.** For each known platform, we have one
   "this must work" check. For GitHub: can we locate the profile
   menu in `interactive`? For Linear: is there a sidebar with
   workspace nav? If the test passes, we're genuinely on that
   platform. If it fails, the domain match was misleading.

**Output — one of four verdicts:**

| Verdict | When | What happens next |
|---|---|---|
| `known_confident` | All three signals agree | Plan with specialist prompt, skip exploration |
| `known_similar` | Domain matches, canonical test fails | Explore (maybe it's an enterprise variant) |
| `unknown` | No signals match | Explore (task-guided) |
| `unknown_hostile` | Interactive list is nearly empty OR inner_text is nearly empty OR page is mostly canvas/iframe | Visual fallback (Tier 3) |

---

## Step 7a — KNOWN platform: plan directly

If `recognize = known_confident`, the planner (Opus) writes the plan
without needing to look at the page. It has strong priors for this
platform.

**What goes into the planner prompt:**

- A specialist system prompt for the `task_type` (~200 words about the
  task shape — e.g., "An extract task is 3-5 steps: goto, screenshot,
  extract, done.").
- A platform-specific overlay if we have one (e.g., "On GitHub, the PR
  list has infinite scroll, not pagination. Use the sidebar to filter,
  not URL parameters.").
- The task prompt.
- The input row.
- The start URL.
- The extract schema.

**What the planner is NOT given:**

- The page snapshot. Planner is writing a PLAN; it doesn't yet know
  what's on the page. It writes plan steps in terms of semantic targets
  ("click the PR list link"), and the act node resolves those against
  the actual interactive list at runtime.

**What the planner outputs:**

A list of plan steps. Each step says:

- `action`: goto / click / type / screenshot / extract / done.
- `target`: URL / selector / text / mark_id / schema key.
- `value`: for type actions.
- `rationale`: optional human-readable reason.

Example for Linear:

1. `{action: goto, target: "{{input.url}}"}`
2. `{action: screenshot, target: "ticket_page"}`
3. `{action: extract, target: "fields"}`
4. `{action: done, target: "ok"}`

This plan gets cached. Sample 2 through 1000 of the same task reuse it
without calling Opus again.

---

## Step 7b — UNKNOWN platform: explore first

If `recognize ∈ {known_similar, unknown}`, we DON'T plan yet. We
explore.

**The explore node goes like this:**

1. We already have the first snapshot (from preflight). Good.
2. Send Opus a task-guided exploration prompt:
   - "Task: {task_prompt}. Input row example: {first row of CSV}."
   - "Current page snapshot: {title + outline + top 20 interactive items}."
   - "Which 3 nav items or links best match the task intent? Return
     them with one-sentence reasoning each."
3. Opus picks 3.
4. For each of the 3: click it, wait for stability, snapshot, save
   snapshot to corpus.
5. If any of the 3 lead to a URL that opens another obvious nav
   pattern (a list of detail views, for example), click one item in
   that list too.
6. Stop when budget runs out (60 seconds, 5 pages max) or when
   exploration stops revealing new DOM structural patterns.

**Then we synthesize a profile.** Opus takes the corpus of snapshots
and produces a small JSON object with **motor programs**:

- `to_reach_detail_view`: click an element whose role is `row` or
  `link`, whose accessible name contains the ticket_id input value.
  Verification text = ticket_id.
- `to_paginate`: scroll down until no new rows appear, OR click a
  button labeled "Next" / "Load more."
- `to_filter`: type into the input with role=searchbox.
- `assignee_locator`: look for a text node that is a sibling of the
  label "Assignee" or has aria-label="Assignee."

These are CONCRETE action recipes, not descriptions.

**Now the planner runs, with the profile as grounding.** Opus writes
plan steps that reference motor programs:

1. `{action: goto, target: "{{input.url}}"}`
2. `{action: screenshot, target: "detail_page"}`
3. `{action: extract, target: "fields", hints: {assignee_locator: "..."}}`
4. `{action: done}`

---

## Step 7c — HOSTILE DOM: visual fallback

If `recognize = unknown_hostile`, the DOM doesn't have enough
grounding. Examples: a Tableau dashboard where content is canvas, an
embedded PDF viewer, a map UI where clicks are on SVG paths without
accessible names.

**What changes:**

- Instead of relying on `interactive` + `inner_text`, we render a
  full-page screenshot.
- We overlay numbered bounding boxes (Set-of-Mark) on every detected
  interactive region. Detection uses the same signals as before, plus
  a geometric pass that finds areas with event handlers inside canvas
  where possible.
- We OCR the text inside each box for a legend.

**The planner now sees:**

- The screenshot (as a multimodal input).
- A text legend: `{box_id: 1, label_ocr: "View Details", role: link}`, etc.
- The same task + schema + specialist overlay.

**Plan steps change:**

- `action: click, target: box_id=7, verification_text: "View Details"`.
- At act time, the tool resolves the box by ID to a pixel coordinate
  and clicks there.

**Extraction changes:**

- LLM reads the screenshot for values.
- It must return `{field: value, cited_region: {x, y, w, h}}`.
- A second LLM call crops that region and re-reads it to confirm.
- Disagreement flags the field, rather than trusting one read.

This tier is slower (vision tokens are expensive, ~3x the cost) but
it's the only thing that works when text-first grounding fails.

---

## Step 8 — Self-test gate (unknowns only)

Whether we built a profile via Tier 2 (explore) or Tier 3 (visual),
we DO NOT trust it for 1000 samples.

We run sample 0 end-to-end using the new profile. The extract node
runs, the judge runs, we get a verdict.

- **Judge says pass?** → cache the profile, unlock samples 1-999.
- **Judge says fail?** → demote to Tier 4 (teach mode).

---

## Step 8b — Teach and learn (Tier 4)

If the self-test fails, the dashboard tells the operator: "We can't
plan this platform automatically. Please walk through sample 1."

**What happens:**

1. A non-headless Chromium opens. The operator sees a real browser.
2. They navigate to the start_url, click through to the right page,
   find the answer, and mark it (literally click a "this is the
   assignee" button in our overlay). They do this for each schema field.
3. Behind the scenes, Playwright records a trace:
   - Every navigation (URL, timestamp).
   - Every click (CSS selector it resolved, accessible name of the
     clicked element, coordinates).
   - Every type (selector, value).
   - A screenshot before and after each action.
   - DOM state at each step.
4. When the operator clicks "Done," we have a full recording of what
   they did.

**Abstraction step:**

Opus takes the trace + the input row the operator used + the full
input CSV. It parameterizes the trace:

- Wherever the operator typed or clicked a value that matches a field
  in the input row, replace with a variable.
- Wherever the operator highlighted a value as an answer, mark that
  node's selector as an extraction target for that schema field.

Result: an abstracted trace that is essentially a plan + extraction
map, parameterized by input.

**Validation step:**

We replay the abstracted trace on sample 2 (different data). If it
runs cleanly and the extraction produces schema-valid output, we
cache it. If it breaks, we escalate (Tier 5 — operator handles each
sample manually).

---

## Step 9 — The act/observe/verify loop

Now planning is done. The LangGraph loop takes over. For each plan
step:

### 9.1 act

Execute the step using BrowserTools. Tools are deterministic:

- `goto(url)` calls `page.goto` under the rate limiter.
- `click(selector or text)` resolves the target in the current
  `interactive` list and calls Playwright's click.
- `type(selector, value)` calls `page.fill`.
- `extract(schema)` — this is a minimal scaffold; real extraction
  happens in the extract node.
- `done` ends the loop.

If the target doesn't resolve (no matching element, ambiguous text),
the tool returns a `ToolResult{status: error}` and the agent records
`last_tool_error`. Verify will short-circuit to fail without even
calling the LLM.

### 9.2 observe

After any act except `done`, we take a new snapshot (Step 3 again).
We append it to the observations list.

Critically: **we compact observations on every append.** If there are
more than 5 snapshots, older ones get replaced with a one-line
breadcrumb (`snapshot.abstract: "title @ url"`). But **extract
entries are never compacted** — those carry per-item data that the
final extractor needs verbatim.

### 9.3 verify

Sonnet reads:

- The task prompt.
- The current plan step that just executed.
- The last tool call's result.
- The fresh post-action snapshot (title, URL, inner_text truncated to
  2000 chars, interactive element names).

It returns `{ok: bool, reason: string}`.

- `ok = true` → advance to next step.
- `ok = false` + `consecutive_fails < 2` → retry the same step.
- `ok = false` + `consecutive_fails == 2` → escalate to replan.
- `reflect_count >= 3` → abort the sample with reflection cap reached.

---

## Step 10 — Extract: pulling the actual values out

After all plan steps complete, the extract node fires.

**What it gets:**

- The task prompt.
- The extract schema.
- The accumulated observations (compacted, but with all extract
  entries preserved).

**The observations are projected for the extractor prompt:**

For each observation entry:

- If it's a snapshot: send `{url, title, inner_text[:2000], interactive names only}`.
- If it's an extract entry (from an earlier list_iter step): send the whole entry.
- If it's a snapshot.abstract breadcrumb: send the one-line summary.

This keeps the extractor's context bounded. For a 60-step list_iter
task, the extractor sees: 60 extract entries (complete) + ~5 snapshots
(full) + ~55 abstract breadcrumbs (1 line each) = a few dozen KB of
input, not a few MB.

**What the extractor does:**

1. First attempt: Haiku gets the prompt + schema, returns JSON.
2. We jsonschema-validate the output.
3. If invalid: retry with errors in the prompt + the prior attempt
   visible. Up to 2 retries total.
4. If a field needs deterministic parsing (a date, a currency, an
   email), the schema declares this. The LLM locates the value; code
   parses it; if parse fails, flag the field.

**How the LLM finds the right value:**

The schema gives it field names. The observations give it text. It
matches semantically — "assignee" field gets looked up by finding the
label "Assignee" and reading its sibling text, or by pattern-matching
"assigned to X" in inner_text.

For known platforms, the specialist prompt includes field hints
("assignee lives in the properties panel"). For unknown platforms,
the profile's motor programs (`assignee_locator`) are injected.

---

## Step 11 — Handling long pages

This is where "but pages can get long" gets answered.

### 11.1 Snapshot-level truncation

Already happening in Step 3:

- `inner_text`: visible text only, script/style excluded, whitespace
  collapsed. A 200-comment GitHub issue might produce 50 KB; a
  typical page is 5-20 KB.
- `interactive`: the full list, but it's small (just labels and
  selectors, not full HTML). 200 items ≈ 15 KB.
- `outline`: a few hundred bytes, always.

### 11.2 Observation compaction

Already happening in Step 9.2:

- Snapshots older than the most recent 5 get replaced by a 1-line
  breadcrumb.
- Extract entries never compacted.

### 11.3 Extractor payload projection

Already happening in Step 10:

- `inner_text` truncated to 2000 characters per snapshot when fed to
  the extractor.
- Interactive list reduced to just names.
- Full extract entries kept.

### 11.4 What if the needed value is beyond 2000 characters?

Two strategies:

1. **Plan narrower extraction.** If the value is in a specific
   section, the plan step should click into that section (reducing
   the page to the relevant detail view) rather than trying to
   extract from the whole page.
2. **Anchor-based truncation.** The extractor is prompted with "look
   for the value near the text 'Assignee:'" — we can build the
   payload to include a window AROUND the anchor rather than the
   first 2000 characters.

This is configurable per field in the schema's extraction block.

### 11.5 Visual tier and long content

For hostile DOM, we don't rely on inner_text at all. The screenshot
is finite (viewport-sized or full-page, up to a cap). Set-of-Mark
boxes are bounded by the interactive count. Vision tokens are the
cost ceiling, not page length.

---

## Step 12 — When we use LLMs vs when we don't

Clear map.

### Always deterministic (no LLM):

- Interactive element detection (Step 3.4).
- inner_text extraction (Step 3.2).
- DOM fingerprint computation (Step 6).
- Outline derivation (Step 3.3).
- Page stability check (Step 2).
- Tool dispatch — goto, click, type, screenshot (Step 9.1).
- Schema validation at extract (Step 10).
- Deterministic parsers for dates, emails, currency (Step 10).
- Selector resolution (matching an interactive list entry by role + name).
- Rate limiter token acquisition.
- Auth session load from sealed blob.

### LLM calls (with which model):

| Node | Model | What it decides |
|---|---|---|
| `classify` | Haiku | Task shape (extract / form_fill / …) |
| `recognize` (canonical test only) | Haiku | Does a canonical element exist? |
| `plan` | Opus | The ordered plan step list |
| `explore` | Opus | Which nav items match task intent |
| `profile_synthesis` | Opus | Motor programs from exploration corpus |
| `verify` | Sonnet | Did the last action succeed? |
| `extract` (locate phase) | Haiku | Which DOM node or text anchor for a field |
| `judge` | Opus | Final pass/fail verdict on extracted output |

Notice: `act` doesn't call an LLM at runtime. It executes what the
plan says. The plan was written by an LLM, but once written, execution
is deterministic.

---

## Step 13 — How we know the final answer is right

Three layers of check, each catches different mistakes.

### 13.1 Schema validation

The extractor's output must conform to the extract_schema — field
types, required fields, enum constraints, format constraints (date
pattern, email pattern). Failure → retry up to 2 times with errors
inlined → if still failing, flag.

### 13.2 Deterministic parsers

For typed fields (date, currency, URL, email), the LLM locates the
value but code parses it. If parsing fails, flag.

### 13.3 Judge

Opus reads the task, the extracted output, and the evidence (the
screenshots we captured along the way). It says pass / fail /
uncertain with reasoning.

- `pass` → write to output.csv, move on.
- `fail` or `uncertain` → if reflect_count < 3, re-run extract with
  judge's critique in the prompt. If out of budget, write the row to
  output.csv with the failing verdict (not silently passed; auditor
  sees the failure).

---

## Step 14 — A concrete end-to-end trace

Let's run sample 1 of the Linear task.

**Input:** `{url: "https://linear.app/a/issue/LIN-1"}`. Task:
"Extract ticket_no and assignee." Schema: `{ticket_no: string,
assignee: string}`.

**Pod state:** has the Linear auth blob loaded, proxy IP bound.

**Step 0-2:** context opens, goto `https://linear.app/a/issue/LIN-1`,
wait for stability (700ms).

**Step 3 — first snapshot:**

- URL: `https://linear.app/a/issue/LIN-1`
- Title: `LIN-1 · Implement auth flow · Andera`
- outline: `[h1: "LIN-1 - Implement auth flow"] [h2: "Description"] [h2: "Activity"] [h2: "Properties"]`
- interactive: ~45 items (sidebar links, comment buttons, properties panel controls)
- inner_text: ~6 KB starting with "LIN-1 Implement auth flow..."

**Step 4:** separation already done — 45 things you can click, the
rest is data.

**Step 5 — classify:** task_type = "extract" (Haiku call, ~100ms).

**Step 6 — recognize:**

- Domain `linear.app` is in known list → try canonical test.
- Canonical test: can we find `[role=navigation]` with Linear's
  workspace nav? YES.
- Verdict: `known_confident`.

**Step 7a — plan:** Opus is called with the Linear specialist prompt.
Plan written:

1. goto `{{input.url}}`
2. screenshot `"ticket_page"`
3. extract `"fields"`
4. done

Plan gets cached. Samples 2-1000 reuse it.

**Step 9 — act loop:**

- Step 0 (goto): already on the page. Tool is a no-op-if-already-there.
- observe: fresh snapshot (same as Step 3, basically).
- verify: Sonnet says ok=true ("we are on the expected URL, the H1
  matches a Linear ticket pattern").
- Step 1 (screenshot): takes a PNG, stores content-addressed in
  `runs/<id>/blobs/<sha>.png`. observe + verify pass.
- Step 2 (extract) + Step 3 (done): act routes to extract node.

**Step 10 — extract:**

- Extractor sees: the compacted observations, the schema (ticket_no,
  assignee required).
- It scans inner_text for "ticket_no": the h1 contains "LIN-1" → that's
  the ticket_no.
- It scans for "assignee": finds the Properties panel label "Assignee"
  followed by text "Jane Smith" → that's the assignee.
- Output: `{ticket_no: "LIN-1", assignee: "Jane Smith"}`.
- Schema-validates. Passes.

**Step 13 — judge:**

- Judge reads task, extracted output, evidence (the screenshot).
- Confirms: the screenshot shows "LIN-1" as the ticket identifier in
  the header, "Jane Smith" next to the "Assignee" label.
- Verdict: pass.

**Step end:** row written to output.csv:
`LIN-1,Jane Smith,pass,runs/<id>/blobs/<sha>.png`.

Total time: ~4-6 seconds. LLM calls: classify (cached after sample 1),
plan (cached after sample 1), verify × 2, extract × 1, judge × 1.

---

## Step 15 — When things go wrong

Quick tour of the failure modes and what happens.

### Page doesn't load

- goto times out → ToolResult is error → verify short-circuits to ok=false.
- Retry same step up to `consecutive_fails=2`. If still failing, replan.
- If replan doesn't help (`reflect_count=3`), abort sample with
  `reflection cap reached`.

### Clicked wrong element

- Act succeeds (we did click something).
- observe shows page didn't change as expected.
- verify says ok=false (Sonnet notices the mismatch).
- Retry / replan per budgets.

### Extracted wrong value

- Schema-validates (it's a string, it's non-empty).
- Judge catches it: "the evidence screenshot shows the assignee is
  Jane Smith, but the extracted value is John Doe. fail."
- Extract re-runs with judge's feedback. Up to reflect_count.

### Session died mid-run (Linear kicked us out)

- observe shows login wall.
- `auth_check` node (post-observe) classifies "login_wall."
- Sample aborts with `status=auth_died`.
- Pod releases the auth group lease.
- Scheduler parks the group; operator alerted.
- When operator re-authenticates (`andera login`), the group resumes.

### Profile drifted (site changed overnight)

- observe computes DOM fingerprint.
- Doesn't match cached fingerprint in profile.
- Group pauses. Sample abort-with-replay (it'll rerun after profile
  rebuild).
- Explore + self-test + canary rebuild the profile.
- Group resumes.

### Long page (500-comment thread)

- inner_text is ~200 KB raw.
- Snapshot keeps it all in state (for possible later plan step).
- Extractor payload truncates to 2000 chars OR uses anchor-based window.
- If the value is beyond the window, plan should have added a
  scroll/filter step to narrow down; if it didn't, verify or judge
  catches the missing field and replan picks a better approach.

---

## One-page summary

What gets read from the page, always: URL, title, inner_text,
outline, interactive list, page state. Pre-computed deterministically.

Clickable vs data: pre-separated at snapshot time using ARIA roles,
HTML tags, event handlers, tabindex, and cursor style. LLM never guesses.

LLM calls: classify (shape), recognize (platform), explore (unknowns),
plan (steps), verify (did it work), extract (which value where),
judge (is it right).

Fallback order: known specialist → explored profile → visual
Set-of-Mark → teach-and-learn → human manual.

Long pages: handled at three layers — snapshot extraction filters to
visible content; observations compact old snapshots; extractor
payload truncates per-snapshot but never drops extract entries.

Self-test gate before bulk: any fresh profile runs sample 0 end-to-end
and is only cached if the judge passes. Canary batch of 5 more before
unlocking 995.

The agent never silently produces wrong data. If it can't verify, it
flags. If it can't plan, it demotes. If it can't click the right thing,
it retries or aborts with a specific reason.

That is the whole planning system.
