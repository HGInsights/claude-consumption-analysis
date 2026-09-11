# claude-consumption-analysis

Find out where your organisation's Claude Enterprise bill actually goes — not
just *who* spends, but *why* each workload costs what it does, and whether it is
running on the right model and the right surface.

Works against any Claude Enterprise org. Python 3.9+, standard library only, no
dependencies to install.

---

## ⚠️ Read this before you run anything

**1. This tool can read your employees' actual conversations.** It is built in
three tiers of escalating sensitivity — metadata, then titles, then full
transcripts. Tier 2 pulls what people actually typed. Get Legal and People
sign-off *before* you use it, tell staff *before* rather than after, scope it to
a named investigation rather than standing access, and report in aggregate.
See [Tiers are a privacy boundary](#tiers-are-a-privacy-boundary).

**2. Analysis output is gitignored by default — keep it that way.** `out/`,
`profiles/` and `findings/` contain names, verbatim prompts, customer names and
per-person cost. The `.gitignore` protects those *directories*, not the content
*pattern*: if you write analysis anywhere else, you are one `git add` away from
publishing your colleagues' unfiltered questions. Check `git status` before
every commit.

**3. Per-conversation cost is reconstructed, not measured — and it overstates
agentic sessions by up to 19×.** The tools reject their own estimate past 40%
divergence from billing actuals. That rejection is correct behaviour. See
[What this cannot measure](#what-this-cannot-measure) before you try to "fix" it.

---

## Why this exists

Organisations land on Claude Enterprise and discover a metered bill that nobody
can attribute. The instinct is to build a gateway or replace the UI. That
instinct is usually wrong on the numbers, for two reasons worth knowing before
you spend engineering time:

- **Seats are not the cost.** Claude Enterprise is a per-seat fee *plus* metered
  token usage at API rates, with no bundled allowance. At most headcounts the
  large majority of the bill is inference, not licensing — so rebuilding the UI
  to escape seat fees attacks the smallest line on the bill.
- **No third-party gateway can see first-party chat traffic.** LiteLLM, Portkey
  and friends proxy API calls; they cannot touch Chat, Cowork, or Claude Code.

Meanwhile the telemetry you want already ships with the contract and costs
nothing extra. This toolkit is the "instrument before you build" step: point it
at your org, find the mechanism, then decide whether you have a spend problem or
a pricing conversation.

### Is your per-user spend actually anomalous?

Useful public reference points, because "$2,000/month/user" means nothing without
a denominator:

| Reference point | Figure |
|---|---|
| Ramp — median AI spend per employee per month, all businesses | $46 |
| Microsoft, customer-facing org — median | $134/user/mo |
| Microsoft, AI engineering org — median | $975/user/mo |
| Uber's cap for engineers on agentic coding tools | $1,500/user/mo, approval above |

Sources: [Ramp AI Index](https://ramp.com/data/ai-index-august-2026),
[BI/Yahoo Aug 2026](https://finance.yahoo.com/technology/ai/articles/microsoft-cracks-down-employee-ai-174500002.html),
[TechCrunch Jun 2026](https://techcrunch.com/2026/06/02/uber-caps-employee-ai-spending-after-blowing-through-budget-in-four-months/).

A non-engineering role sitting at or above the median of Microsoft's *AI
engineering* organisation is worth investigating. An engineer running agent loops
at $900/month probably is not.

### The one thing to check before you instrument anything

**Confirm AI usage appears in no leaderboard, OKR, enablement target, or review
criterion.** Uber burned its 2026 AI budget in four months behind an internal
leaderboard ranking teams by AI usage. If one exists in your org, removing it is
free and is probably worth more than everything else in this repo.

---

## The central constraint: cost is reconstructed, not read

Two Anthropic APIs, and neither alone answers the question:

| API | Has | Lacks |
|---|---|---|
| **Enterprise Analytics** `/v1/organizations/analytics/…` | USD cost, token counts, per-user/model/product | any content |
| **Compliance** `/v1/compliance/…` | full transcripts, tool calls, projects, sessions | **no token counts, no cost, anywhere** |

They join only on `(user, date)`. So per-conversation cost is **reconstructed
from transcript shape** and then reconciled against Analytics actuals. That is
the load-bearing design decision — it is what lets the toolkit explain *why* a
conversation was expensive, which the cost API alone never can.

The model in one line:

```
billed_input = Σ over assistant turns i of ( overhead + Σ tokens of turns before i )
```

**Cost is quadratic in conversation length, not linear.** A 40-turn chat re-pays
for its early messages ~20 times. Every headline signal falls out of this
formula: `repayment_multiplier` (billed_input ÷ content_tokens),
`project_overhead_tokens`, `n_cold_resumes`.

---

## The hypothesis tree

Six candidate root causes. Each has a signal that confirms or refutes it, and
each has a *different* fix — which is the entire point of separating them. Keep
new signals traceable to one of these; a metric that doesn't discriminate between
hypotheses is noise.

### H1 — Context re-payment (the quadratic)

Two users sending byte-identical content, one in a single 40-turn thread and one
in five 8-turn threads, differ ~5× on input tokens. Neither did anything visibly
wrong.

- **Signal:** `repayment_multiplier`. Healthy is under 8.
- **Confirms:** median assistant turns per chat > 20, multiplier > 15.
- **Fix:** enablement, not technology — "start a new chat when the topic changes".

### H2 — Fat projects

Every file attached to a Project is re-sent as context on **every message of
every chat in that project**. A project with 300k tokens of attachments costs
real money per turn before anyone types anything.

- **Signal:** `project_overhead_tokens`; projects ranked by attachment weight.
- **Fix:** trim attachments. Modelled recovery ~80% of the overhead.

### H3 — Premium model on trivial work

Premium tiers cost several times Sonnet. Ramp's data shows premium models went
from 5.7% to 55.9% of business AI cost in ten months — the default drifting
upward is the market norm, not a quirk of your org.

- **Signal:** `pct_premium_model` per user (tier 0, no content needed);
  `PREMIUM_ON_TRIVIAL` flag.
- **Fix:** org default model plus role entitlements. Free, reversible, same-day.

### H4 — Cold cache

Prompt caching has a ~5-minute TTL. The gap that kills it is **human think
time**. Ask, read, think for ten minutes, follow up — and you pay full input
price for the whole conversation, every turn.

- **Signal:** `n_cold_resumes` / `n_assistant_turns`.
- **Honest sizing:** caching is quoted as "10× cheaper", but that's the cached
  prefix alone — the newest turn pays full price and **output is never
  cacheable**. Measured blended saving in the fixture is **2.8×, not 10×**.
- **Fix:** mostly not actionable. Useful as an explanation, not a lever.

### H5 — Agentic fan-out

Cowork and Claude Code generate large intermediate token volumes — tool calls,
file reads, subagents. A non-engineer running agent loops over a folder of
documents can out-spend an engineer.

- **Signal:** `top_product`, then session transcripts and tool-call counts.
- **Fix:** surface gating by role, and model choice within the agentic surface.

### H6 — The spend is legitimate

Must stay on the list. If top spenders are doing hard, valuable work on
appropriate models in well-shaped threads, **the finding is that the bill is the
price**, and the action is renegotiation, not restriction.

- **Signal:** low multipliers, few flags, tight model-to-task fit.

---

## Setup

```bash
git clone <this repo> && cd claude-consumption-analysis
cp .env.sample .env      # then fill it in; .env is gitignored
python3 test_reconstruction.py   # 32 offline checks, no credentials needed
```

No `pip install` — the toolkit is standard library only.

### Getting the keys

`.env` takes three values, and **they are not interchangeable**:

| Variable | Where | Notes |
|---|---|---|
| `ANTHROPIC_COMPLIANCE_KEY` | claude.ai → Organization settings → Compliance access key | **Primary Owner only.** Needs `read:compliance_user_data` and `read:compliance_org_data`. |
| `ANTHROPIC_ANALYTICS_KEY` | claude.ai → Organization settings → API | Needs `read:analytics`. |
| `ANTHROPIC_API_KEY` | Console | Only needed for optional classification. |

**The key gotcha that will waste your afternoon.** A plain workspace key
(`sk-ant-api03-…`) is rejected by the Analytics endpoints with a misleading
`401 "x-api-key header is required"`. The header *is* there — this is a **scope**
problem, not a header bug. Likewise an Admin key (`sk-ant-admin01-…`) reaches the
activity feed and nothing else; it will not work for compliance content.

The tools probe for this: if `ANTHROPIC_ANALYTICS_KEY` can't read analytics, they
fall back to the compliance key and print a note.

### Prerequisite

Tier 0 requires a **usage-based** Enterprise contract. Seat-based legacy
contracts expose usage credits only, and the cost endpoints will be empty.
Verify this first — it blocks everything else.

---

## Running it

Start at the lowest tier that answers your question, and stop there.

```bash
# Tier 0-1 — metadata only, no transcripts read
python3 investigate_user.py someone@example.com --days 30

# Cohort sweep across many users
python3 cohort.py --days 30 --emails a@example.com b@example.com
python3 cohort.py --days 30 --file cohort.txt

# Tier 2 — reads actual conversations. Needs sign-off.
python3 investigate_user.py someone@example.com --days 30 --transcripts
python3 cohort.py --days 30 --file cohort.txt --transcripts

# One-shot structured audit of a single user (JSON out, for automation)
python3 analyze_user.py someone@example.com --days 30 --out out/audits
```

Useful flags: `--max-chats`, `--max-sessions` to bound the transcript pull,
`--out` to redirect output.

`investigate_user.py` prints a full diagnosis and writes JSON plus a per-chat
CSV. `cohort.py` ranks a group and assigns each user a pattern verdict.
`analyze_user.py` emits one consistent JSON blob per user, designed to be run
unattended across a batch.

### Configuring retrieval-tool detection

Duplicate-call detection needs to know which of your tools are *read-only*, since
calling those twice with identical input is waste rather than work. Built-in
Claude tools and common connector verbs are recognised by default, plus a
prefix heuristic (`search_`, `get_`, `list_`, `lookup_`, `fetch_`, `query_`,
`read_`).

For MCP servers whose names don't follow that pattern, create
`retrieval_tools.txt` next to the scripts — one bare tool name per line,
`#` comments allowed. It is gitignored, so your internal tool names never reach a
commit.

---

## Tiers are a privacy boundary

Not a convenience. Each tier reads strictly more sensitive data than the last.

**Tier 0** — Analytics only. Cost per user, model mix, concentration. No content,
no review needed. *This may be all you need:* if your top 10 users are 70% of
spend and 80% premium-model, H3 is your answer and you can act on Monday.

**Tier 1** — adds project attachment weights and per-turn cost. Metadata only
(titles, file sizes). Tests H2 directly.

**Tier 2** — pulls transcripts. **This reads employees' actual conversations.**

If you run tier 2, these are the rules that keep it legitimate:

- Legal and People sign-off first; tell people before, not after.
- Scope to a named investigation, not standing access.
- Report in aggregate. Don't circulate individual transcripts.
- **Classify then discard.** Don't warehouse transcripts — retaining prompts and
  outputs turns every employee's unfiltered questions into discoverable records,
  and carries security and cost downsides of its own.
- The optional classifier sends only the **first and last user turn** per chat.
  Keep it that way.
- **Interview your top 5 spenders before drawing conclusions from their data.**
  It's faster, it's fairer, and they will tell you things the transcripts won't.

---

## What this cannot measure

Be honest about these when you present findings.

- **Per-conversation cost is reconstructed, and overstates agentic sessions by up
  to 19×.** The quadratic model assumes a human re-paying for context each turn.
  In a long agentic tool loop the prefix stays warm, so the re-payment is billed
  at the cache-read rate, not full price. The tools reconcile against Analytics
  and **reject their own estimate past 40% divergence**, printing the cache-hit
  rate as the cause. When that fires, per-conversation dollars are not usable —
  but the *shape* findings (turn counts, tool calls, model mix) remain valid,
  because those are measured rather than reconstructed. **Trust Analytics for
  dollars.** Don't tune constants until the warning stops firing.
- **Token counting is a ~3.9 chars/token heuristic**, deliberately. Good enough
  to rank users and size 10–50× effects. Swap in the `count_tokens` API only if
  you genuinely need precision.
- **Cost data lags.** Analytics refreshes ~every 4h with up to 24h delay, and is
  revised for 30 days. The tools end their window **2 days back** to avoid
  provisional numbers.
- **Transcripts are partly redacted.** Thinking blocks, system prompts, tool
  definitions, MCP config, images/PDFs and citation metadata do not come through.
  Text, tool calls and tool results do.
- **Coverage gaps.** Claude Code via Bedrock/Vertex/Foundry, Claude Code on the
  web, and Console-API-key sessions are absent from Compliance session data. ZDR
  and HIPAA-readiness orgs capture nothing at all.
- **Unit economics are out of scope.** Nothing here tells you whether
  $2k/month/user is *worth it*. That needs revenue data this toolkit never sees.

---

## Reference: the Compliance API

Enterprise-only, exposing your org's actual Claude content for compliance,
eDiscovery and DLP. Separate key, separate scopes, not the usage API.

| Endpoint | Gives you |
|---|---|
| `GET /activities` | Org-wide event feed: actor email, user ID, IP, user agent, event type |
| `GET /apps/chats` | Every chat: id, title, **model**, project_id, user, timestamps, `href` |
| `GET /apps/chats/{id}/messages` | Full transcript — every turn, attached and generated files, artifacts |
| `GET /apps/projects` + `/{id}/attachments` | Projects and attachments, with `size_bytes` |
| `GET /apps/sessions/local` + `/{id}/messages` | Cowork, Claude Code, Office agents — transcripts including tool calls and results |
| `GET /apps/sessions/remote` | Cowork on web/mobile |
| `DELETE …` | Content deletion, with `delete:compliance_user_data` |

Rate limit 600 req/min; retention 6 years by default. **Pagination differs by
endpoint family** — chats use `after_id`/`has_more` cursors, sessions and
projects use `page`/`next_page`. The clients here handle both.

---

## Files

```
investigate_user.py       full single-user diagnosis: clients, cost reconstruction,
                          flags, counterfactuals, hypothesis verdicts
cohort.py                 rank a group of users, assign pattern verdicts
analyze_user.py           one-shot structured JSON audit, for unattended batches
test_reconstruction.py    32 offline checks against synthetic transcripts
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The privacy rules there are not
negotiable; the cost model is very much open to improvement.

## Licence

[Apache-2.0](LICENSE).
