# PRD — AI Spend Visibility & Cost Control at HG Insights

**Owner:** Francis Brero
**Status:** Draft for review — decision document, not a build authorization
**Date:** 9 September 2026
**Current run-rate:** ~$40,000/month (~$480,000/year) on Claude Enterprise

---

## 1. Executive summary

**Recommendation: do not build a gateway or rebuild the UI yet. Spend the next 30 days instrumenting what you already pay for, then re-decide with data.**

Three findings should change how this problem is framed:

1. **The seats are not the cost.** Claude Enterprise is $20/seat/month plus metered token usage at API rates — there is no bundled token allowance. At any plausible headcount, **80–92% of your $40k is inference, not licensing.** Rebuilding the UI to escape seat fees attacks the smallest line on the bill. ([pricing](https://claude.com/solutions/enterprise))

2. **The telemetry you want already exists and is unbilled.** Anthropic ships an **Enterprise Analytics API** (per-user, per-model, per-product token usage *and* USD cost) and a **Compliance API** (actual chat content, session transcripts, per-session usage across Chat, Cowork, and Claude Code). Between them you can answer "who, which model, what task, how much" without writing a proxy. This is a 2–3 week integration, not a platform build.

3. **A gateway that reaches the chat surface does exist — Anthropic's own.** The self-hosted **Claude apps gateway** puts Claude Desktop's Chat, Cowork, and Code tabs behind your IdP, with per-group model allowlists, per-user spend limits, and OTLP telemetry to your own collector. No third-party gateway (LiteLLM, Portkey, Cloudflare, Kong) can touch first-party chat traffic — that part of your instinct was correct. But Anthropic's can, for the desktop app.

The honest read on the LibreChat/Open WebUI option: it costs **$363k–815k in year one** and **$300k–550k/year steady-state** to recover **~$60k/year in seat fees**, while losing Cowork, Claude in Excel, mobile apps, Artifacts sharing, and the connector ecosystem — precisely the surfaces your CSMs and PMs use. It is defensible only as a *second* system for specific high-volume workloads, never as a replacement.

**Modelled outcome of the recommended path: ~41% run-rate reduction (~$200k/year) for ~$100k of one-off effort, with no loss of capability.** Details in §6.

---

## 2. Problem statement

### 2.1 What's actually wrong

The presenting complaint is cost. The underlying problem is **three separate deficits that are being treated as one**:

| Deficit | Symptom | Is it real? |
|---|---|---|
| **Attribution** | "We don't know who's spending what" | ⚠️ Solvable today, natively. See §4. |
| **Task visibility** | "We don't know what they're doing with the tokens" | ⚠️ Largely solvable today. See §4. |
| **Control** | "We can't stop a CSM burning $3k/month on Opus" | ⚠️ Solvable today, natively. See §4. |
| **Unit economics** | "We don't know if $2k/month/CSM is worth it" | ❌ Genuinely unsolved — and not solvable by any gateway. See §8. |

Only the fourth requires new thinking. The first three are configuration and integration work against capabilities that shipped in the last two quarters.

### 2.2 Benchmarking: is $1,000–3,000/user/month anomalous?

**Yes — materially, as a job-function mismatch rather than as an absolute number.**

| Reference point | Figure | Source |
|---|---|---|
| Microsoft, Customer & Partner Solutions (closest analogue to CSMs) — median | **$134/user/mo** | [BI/Yahoo, Aug 2026](https://finance.yahoo.com/technology/ai/articles/microsoft-cracks-down-employee-ai-174500002.html) |
| Microsoft, CoreAI (AI engineers) — median | $975/user/mo | ibid. |
| Uber's cap for engineers on agentic coding tools | **$1,500/user/mo**, approval required above | [TechCrunch, Jun 2026](https://techcrunch.com/2026/06/02/uber-caps-employee-ai-spending-after-blowing-through-budget-in-four-months/) |
| Ramp — median AI spend per employee per month, all businesses | $46 | [Ramp AI Index](https://ramp.com/data/ai-index-august-2026) |
| Atlanta Fed — top-decile firm plan, 2026 | $2,800/employee/**year** | [Atlanta Fed, May 2026](https://www.atlantafed.org/research-and-data/publications/policy-hub-macroblog/2026/05/06/how-much-firms-spending-on-ai-and-what-will-happen-to-headcounts) |

Your CSMs at $1–3k/month are sitting **at or above the median of Microsoft's AI engineering organisation**, and at or above the ceiling Uber imposes on engineers running parallel coding agents. Nothing in the public record describes a CSM or PM workflow that requires that consumption profile.

**The likely mechanism is mundane, and it is testable.** Two behaviours produce exactly this spend curve from ordinary chat usage with no agentic workload at all:

- **Long-context bloat** — never starting a new conversation, so every turn re-pays for a growing context window.
- **Premium-model default** — Ramp's data shows premium models went from 5.7% to **55.9%** of business AI cost between June 2025 and April 2026. If your org default is Opus or Fable, every "summarise this email" costs 2.5–5× what it needs to.

Both are visible in the Analytics API within days of turning it on. Both are fixable with a settings change.

### 2.3 The one thing to check before anything else

⚠️ **Uber's overrun was caused by an internal leaderboard ranking teams by AI usage.** Before building anything, confirm that AI usage does not appear in any HG leaderboard, OKR, enablement target, or performance-review criterion. If it does, removing it is free and is probably your highest-ROI action.

---

## 3. Goals & non-goals

### Goals
- **G1** — Per-user, per-model, per-product cost attribution, refreshed at least daily, exportable to the finance stack.
- **G2** — Task-level classification of what Claude is being used for, at a granularity that supports "are we over-provisioning intelligence against this task?"
- **G3** — Enforceable controls: model defaults by role, per-user and per-group spend limits, alerting before overrun.
- **G4** — A defensible answer to "is this spend producing value?" for at least the CSM population.
- **G5** — Reduce run-rate by ≥30% without reducing capability or adoption.

### Non-goals (explicitly out of scope for now)
- **NG1** — Replacing the claude.ai chat experience for general knowledge workers.
- **NG2** — Multi-vendor model abstraction. You are effectively single-vendor; building a vendor-abstraction layer to solve a cost problem is premature.
- **NG3** — Real-time inline interception of browser claude.ai traffic. Not achievable without TLS interception via a CASB/SSE programme, which is a separate initiative with its own privacy and works-council implications.
- **NG4** — Retaining raw prompts and completions long-term. The FinOps Foundation explicitly advises against this on both security and storage-cost grounds; it also converts every employee's unfiltered questions into discoverable records.

---

## 4. What is actually available today (the reframing)

There are **three distinct interception points**, and conflating them is what makes this problem look harder than it is.

| Surface | Can a 3rd-party gateway see it? | What *can* see it | Effort |
|---|---|---|---|
| **claude.ai in a browser** | ❌ Never. Session-authenticated to Anthropic's backend, private wire protocol, no base-URL override. | Analytics API (cost/usage) + Compliance API (content). CASB/SSE with TLS inspection if you need inline blocking. | Low (API) / High (CASB) |
| **Claude Desktop** (Chat, Cowork, Code tabs) | ❌ | **Anthropic's Claude apps gateway** — full inline routing, your IdP, your telemetry. | Medium |
| **Claude Code / IDE / API clients** | ✅ Yes | Any gateway, or Anthropic's, or Claude Code Analytics API. | Low–Medium |

### 4.1 Anthropic Enterprise Analytics API — the fastest path to G1

`https://api.anthropic.com/v1/organizations/analytics/` — Enterprise-only, Analytics API key generated in claude.ai org settings, 60 req/min.

Provides per-user and org-level:
- Token usage **and USD cost**, broken down by **product, model, context window (0–200K vs 200K–1M), inference region, and speed**
- Chat activity: conversations, messages, projects, files, **artifacts**
- Claude Code: sessions, commits, PRs, lines of code, tool actions
- **Project, skill, and connector usage** ← this is a partial answer to G2, available immediately
- DAU/WAU/MAU, seat counts

Limits to plan around: engagement data lags ~1 day; cost data refreshes every 4h with up to 24h delay and can be **revised for 30 days**; Claude Code via Bedrock is excluded; data only from 1 Jan 2026.

> ⚠️ **Blocking dependency — verify first.** Cost and usage endpoints are available on **usage-based Enterprise plans only**. Seat-based legacy plans see usage credits only. **Confirm which contract HG is on before scoping Phase 0.** ([docs](https://platform.claude.com/docs/en/manage-claude/analytics-api))

### 4.2 Compliance API — the path to G2

`/v1/compliance/*` — Enterprise-only, 600 req/min. Exposes:
- **Activity feed**: event records with actor email, user ID, IP, user agent, event type, resource IDs
- **Content**: chats, files, projects from claude.ai — full message history
- **Sessions**: transcripts from Cowork, Claude Code, Claude Science, Claude for M365, **with per-session usage details**
- **Directory**: users, roles, groups

This is the mechanism for the classification work you're already doing on the Claude Code side, extended to chat. Pull transcripts → classify by task type with a cheap model → join to the cost data from 4.1 → you have "which task categories are consuming which intelligence tier."

> ⚠️ Handle with care. This is retrospective retrieval of employees' actual conversations. Loop in Legal and People **before** the first pull, define retention (classify-then-discard, don't warehouse), and communicate it. Getting this wrong is a bigger risk to the programme than the cost itself. ([docs](https://platform.claude.com/docs/en/manage-claude/compliance-api))

### 4.3 Native admin controls — the path to G3

Already available in the Claude Enterprise admin console, no build required:
- **Model defaults and entitlements by custom role** — set the org default to Sonnet, restrict Opus/Fable to specific roles, cap maximum effort level per role
- **Three-tier spend limits**: org ceiling → group limits via RBAC → individual user caps
- **Spend-threshold alerts** at 75%/90% (admin) and 75%/95% (user), with in-product request-increase flow
- **Surface gating** — e.g. restrict Claude Code to engineering
- **Per-user self-service dashboards** — every user can see their own cost, model breakdown, and progress against limit

Anthropic's own guidance names **Sonnet as the recommended org default**, with Fable reserved for "highest-value, most complex agentic work." ([consumption guide](https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide), [admin controls](https://claude.com/blog/giving-admins-more-visibility-and-control-over-claude-usage-and-spend))

### 4.4 Claude apps gateway — the real answer to "can we route through a gateway?"

A self-hosted service shipped inside the `claude` binary (`claude gateway --config gateway.yaml`).

**What it does:**
- Sits between clients and **Amazon Bedrock / Claude Platform on AWS / Google Cloud / Microsoft Foundry / the Anthropic API**
- Users sign in with **corporate OIDC SSO** — no API keys, no claude.ai account needed; the gateway holds the upstream credential
- **Per-IdP-group model allowlists** and managed settings delivery, replacing the claude.ai admin console for connected clients
- **Per-user and per-group spend limits** with an admin API
- **OTLP/HTTP telemetry** carrying developer identity, token counts, model, and latency to your collector
- **Claude Desktop connects with opt-in** — Cowork and Code tabs by default, and **the Chat tab when you set `chatTabEnabled: true`** (requires Claude Code v2.1.227+ on the gateway server)
- Explicitly **does not log or store prompt or completion content**

**Constraints to design around:**
- **Browser claude.ai is still out of reach.** This covers Claude Desktop only, delivered via MDM (`bootstrapUrl`, `forceLoginGatewayUrl`).
- **OIDC only** — no SAML, no LDAP. One issuer per gateway instance.
- **Linux server only.** Windows unsupported; macOS is dev-only.
- Requires PostgreSQL 14+ (durable spend/audit/identity tables once spend limits are on — back these up).
- **1-hour cache TTL is unavailable** through the gateway; prompt caching falls back to the 5-minute TTL. For long agentic sessions this is a real cost regression that partially offsets the savings.
- Inference bills to your Bedrock/Vertex/Console account. You are trading a Claude Enterprise consumption pool for direct token billing — model the commercial impact before committing.

([docs](https://code.claude.com/docs/en/claude-apps-gateway))

---

## 5. Options evaluated

### Option A — Instrument and govern natively *(recommended)*
Turn on Analytics + Compliance APIs, set model defaults by role, set spend limits, ship showback dashboards.

| | |
|---|---|
| **Effort** | 2–4 weeks, ~0.5 FTE |
| **One-off cost** | ~$40k |
| **Ongoing** | ~0.1 FTE |
| **Capability loss** | None |
| **Addresses** | G1 ✅ G2 ✅ G3 ✅ G5 ✅ |
| **Risk** | Low. Reversible. Worst case you learn where the money goes and stop. |

### Option B — Claude apps gateway for Claude Desktop
Adds inline enforcement, corporate SSO, and OTLP telemetry for Desktop users.

| | |
|---|---|
| **Effort** | 6–10 weeks, ~1 FTE (Linux host, Postgres, OIDC app, MDM rollout, collector) |
| **One-off cost** | ~$80–150k |
| **Ongoing** | ~0.25 FTE + infra |
| **Capability loss** | 1-hour prompt cache TTL; browser users unaffected; requires MDM discipline |
| **Addresses** | G1 ✅ G3 ✅✅ (hard enforcement) |
| **Risk** | Medium. Introduces a Tier-1 dependency: gateway down = Desktop users blocked. Needs HA, on-call, runbook. |

**Verdict: worth a pilot, but only *after* Option A shows where enforcement is actually needed.** Do not build enforcement infrastructure before you know which behaviour you're enforcing against.

### Option C — Third-party gateway (LiteLLM / Portkey / Cloudflare / Kong)
| | |
|---|---|
| **Coverage of your problem** | **~0% of the $40k.** These cannot see claude.ai or Claude Desktop traffic. |
| **Where they *are* useful** | Your product's own LLM calls, batch pipelines, and any future multi-vendor posture. Different budget, different problem. |
| **If pursued anyway** | Cloudflare AI Gateway is effectively free and now does identity-aware attribution (beta). LiteLLM has the best budget/virtual-key model and MCP governance, but a **materially bad 2026 security record** — a PyPI supply-chain compromise in March 2026 plus four critical/high CVEs including an unauthenticated auth bypass on the MCP endpoint. Pin versions, use signed Docker images only, keep it off the public internet. |
| **Avoid** | Percentage-of-spend gateways (OpenRouter 5.5%, Requesty 5%). At your volume that's $1.7–2.2k/month for routing. |

**Verdict: out of scope for this problem. Revisit if HG goes genuinely multi-vendor.**

### Option D — Rebuild the UI on LibreChat / Open WebUI with BYO inference

Evaluated honestly, as requested. The case *for* is real: LibreChat is MIT-licensed, has the strongest subagent and MCP orchestration in the category, and its **Skills implementation is Anthropic `SKILL.md`-compatible with GitHub repo sync** — arguably better-governed than claude.ai. Model routing on BYO inference is a legitimate lever on the part of the bill that matters.

The case *against* is stronger:

**The economics don't work.** You'd spend **$363–815k in year one** and **$300–550k/year steady-state** to eliminate **~$60k/year in seat fees**. Inference cost is unchanged — you pay Anthropic the same API rates through LibreChat as through claude.ai. The only way the maths flips is via routing savings on the inference pool, and **you can capture most of those inside Claude Enterprise for free** with model entitlements by role.

**The parity gap lands precisely on your users.** Total losses: **Cowork**, **Claude in Excel / M365**, **Claude in Chrome**, **native mobile apps**, and the ~439-connector directory. Large gaps: Artifacts (you get a renderer, not shareable/versioned/commentable published artifacts), Projects, memory quality, same-day access to new models. Engineers wouldn't miss most of this. **CSMs and PMs — living in Excel and PowerPoint, working from phones between customer calls, sharing artifacts as lightweight internal tools — will.**

**Hidden costs people underestimate:** internal chat becomes a Tier-1 service with on-call; a breaking-change upgrade treadmill every 6–8 weeks (Open WebUI has a [documented migration failure on large Postgres deployments closed without a fix](https://github.com/open-webui/open-webui/issues/24129)); per-user OAuth tokens for Salesforce/Drive/Slack in your own datastore as a new breach surface; every model launch becomes an integration ticket with 300 people asking why they don't have it.

**Two governance flags:**
- **Open WebUI is not open source** for your scale. Since v0.6.6 its licence prohibits altering or removing "Open WebUI" branding above **50 users in any rolling 30-day period** without a paid enterprise licence (unpublished pricing). Any HG branding requires a commercial deal — reintroducing the per-seat vendor dependency you're trying to escape. ([licence](https://docs.openwebui.com/license/))
- **LibreChat was acquired by ClickHouse in November 2025.** MIT is irrevocable for shipped code, but future feature-gating behind ClickHouse Cloud is a live risk, and there is no published support SKU. ([announcement](https://clickhouse.com/blog/clickhouse-acquires-librechat))

**I found no published account of any enterprise migrating a large non-engineering population off claude.ai or ChatGPT Enterprise to a self-hosted UI and reporting the result.** That absence at this specific shape is itself evidence.

**Verdict: reject as a replacement. Reconsider only as a targeted second system** — 30–50 seats, for MCP orchestration against HG's own data and subagent pipelines where power users and the data team are the audience. That's a ~0.25 FTE project that builds the operational muscle honestly and gives you a real migration option in 12 months instead of a theoretical one.

---

## 6. Cost model

**Assumptions — replace with actuals.** 250 seats at $20/month. Adjust §6.1 once you have the real number; the *shape* of the conclusion holds across 150–400 seats.

### 6.1 Where the $40k actually goes

| Seats | Seat fees/mo | Inference/mo | Inference share | Avg all-in/user/mo |
|---|---|---|---|---|
| 150 | $3,000 | $37,000 | **92.5%** | $267 |
| **250** | **$5,000** | **$35,000** | **87.5%** | **$160** |
| 400 | $8,000 | $32,000 | 80.0% | $100 |

Note the average all-in per user is $100–267/month. **If individual CSMs are at $1,000–3,000, this is a concentration problem, not a broad-base problem.** Expect a small number of users and workflows to account for most of the inference pool. That is good news — it means targeted intervention beats blanket policy.

### 6.2 Model price ratios (list, per MTok)

| Model | Input | Output | vs Sonnet 5 |
|---|---|---|---|
| Fable 5.1 / Mythos 5.1 | $10 | $50 | **5.0×** |
| Opus 5 | $5 | $25 | **2.5×** |
| Sonnet 5 | $2 | $10 | 1.0× |
| Haiku 4.5 | $1 | $5 | 0.5× |

Prompt caching: 5-min cache write 1.25× base input, cache read **0.1×** (0.025× on Fable/Mythos 5.1). **Cached reads are 10–40× cheaper than uncached input.** Batch API is a further 50% off. ([pricing](https://platform.claude.com/docs/en/about-claude/pricing))

### 6.3 Scenario ladder (base case: 250 seats, $420k/yr inference)

| Phase | Intervention | Annual saving | Run-rate after | One-off cost |
|---|---|---|---|---|
| A0 | Do nothing | $0 | $420,000 | $0 |
| A1 | Instrument (Analytics + Compliance APIs) | $0 | $420,000 | $25,000 |
| A2 | Org default → Sonnet; Opus/Fable by role & exception | **$147,000** | $273,000 | — |
| A3 | Per-user spend limits + showback dashboards | $27,300 | $245,700 | $15,000 |
| A4 | Workflow redesign, caching discipline, batch where applicable | $24,570 | $221,130 | $60,000 |
| | **Total** | **~$199,000/yr** | **$221,130** | **~$100,000** |

**All-in run-rate: $281k/year vs $480k today — a 41% reduction, for ~$100k of one-off effort, with zero capability loss.**

### 6.4 Sensitivity and honesty about these numbers

⚠️ **The 35% model-mix saving in A2 is the load-bearing assumption and it is unverified.** It is derived from the 2.5× Opus→Sonnet price ratio combined with Ramp's finding that premium models grew to 55.9% of business AI cost. **If your org default is already Sonnet, this line collapses and the whole business case weakens substantially.** Phase 0 exists specifically to test it before you commit to anything.

Also treat with scepticism:
- **Routing savings**: RouteLLM — the only rigorous public source, and the origin of every "85% savings" claim you'll see — got 85% on MT-Bench but only **45% on MMLU**, and the good results required training the router on labelled data from the target task. Plan on **20–40%** for an untuned router.
- **Semantic caching**: real production hit rates are ~20% for RAG, **10–20% for open-ended chat**, 5–15% for code generation. The "95%" figure circulating is match *accuracy*, not hit *rate*. Anthropic's **native prompt caching delivers more savings than semantic caching for agentic workloads, with far less risk.**
- **Underwrite this programme on governance, attribution, and negotiating leverage.** Treat routing and caching savings as upside to be measured, not committed.

### 6.5 Option D for comparison

| | |
|---|---|
| Seat fees eliminated | $60,000/yr |
| Year-1 cost | $363,000 – $815,000 |
| Steady state | $300,000 – $550,000/yr |
| **Net year 1** | **−$303,000 to −$755,000** |

You would spend 4–8× the savings to capture them.

---

## 7. Phased plan

Each phase has a **decision gate**. Do not start phase N+1 until phase N's gate is cleared.

---

### Phase 0 — Instrument (weeks 1–4) · ~0.5 FTE · ~$25k

**Objective:** replace every assumption in this document with a measurement.

| # | Task | Owner |
|---|---|---|
| 0.1 | **Confirm contract type** — usage-based vs seat-based Enterprise. Blocks everything below. | Finance/Procurement |
| 0.2 | Generate Analytics API key (claude.ai → Org settings → API); build a daily pull into the warehouse | Data Eng |
| 0.3 | Build the attribution dashboard: cost by user, role, product, model, context window | Data Eng |
| 0.4 | **Audit incentives** — confirm AI usage appears in no leaderboard, OKR, enablement target, or review criterion. Remove if it does. | Francis |
| 0.5 | Legal/People review of Compliance API access; define retention (classify-then-discard) and comms | Legal + People |
| 0.6 | Compliance API → pull a **sampled** week of chat sessions for the top 20 spenders | Data Eng |
| 0.7 | Classify sampled sessions by task type with Haiku; join to cost. Reuse the Claude Code classifier taxonomy. | Data/AI |
| 0.8 | Interview the top 5 spenders. Ask what they're doing and why. Do this before drawing conclusions from data. | Francis |

**Gate 0 — you can answer, with numbers:**
- What % of inference spend is Opus/Fable vs Sonnet/Haiku?
- What % of spend sits in the top 10 users? Top 10 workflows?
- What fraction of high-cost sessions are long-context bloat vs genuinely hard tasks?
- Which task categories are running on over-provisioned models?

> **If model mix is already Sonnet-dominant and spend is broadly distributed across genuinely valuable work, stop here.** The correct action is then to renegotiate the contract, not to build anything. That is a legitimate outcome.

---

### Phase 1 — Govern natively (weeks 3–8, overlaps Phase 0) · ~0.3 FTE · ~$15k

**Objective:** pull every free lever before spending engineering time.

| # | Task |
|---|---|
| 1.1 | Set org default model to **Sonnet**. Restrict Opus/Fable to defined roles. Cap max effort level by role. |
| 1.2 | Gate Claude Code to engineering + data. Gate Cowork by role if warranted. |
| 1.3 | Ship **showback before chargeback** — every user sees their own number. Both Uber and the FinOps Foundation did this first, deliberately. |
| 1.4 | Set org spend limit with 75%/90% alerts; group limits via RBAC; individual caps for outliers. |
| 1.5 | Set a **soft** per-person monthly cap with an approval path. Uber uses $1,500 for engineers; a lower figure for GTM roles is defensible. **Do not hard-block.** |
| 1.6 | Enablement: publish "when to start a new conversation," "which model for which task," caching-friendly prompt structure. The long-context-bloat fix is training, not technology. |
| 1.7 | Org instructions to shape default behaviour toward cheaper paths. |

**Gate 1 — 60 days after 1.1 ships:**
- Did run-rate drop ≥25%?
- Did adoption (WAU/MAU) hold?
- Did anyone credibly report degraded output quality?

> **If run-rate dropped ≥30% and adoption held, the case for Phases 2 and 3 largely evaporates.** Bank the win, write the memo, and move on. This is the most likely outcome.

---

### Phase 2 — Gateway pilot (weeks 9–20) · ~1 FTE · ~$80–150k · **conditional**

**Only proceed if Gate 1 showed native controls are insufficient** — i.e. you need inline enforcement, data residency, or telemetry richer than the Analytics API provides.

| # | Task |
|---|---|
| 2.1 | Stand up Claude apps gateway on Linux + Postgres 14+; register OIDC app in Okta/Entra |
| 2.2 | Configure per-group model allowlists and spend limits; wire OTLP to the existing collector |
| 2.3 | Pilot with 20–30 Claude Desktop users; enable `chatTabEnabled` for a subset |
| 2.4 | **Measure the 5-minute-cache-TTL regression** against Enterprise baseline. This can materially offset savings for long sessions. |
| 2.5 | Model the commercial impact of moving inference to Bedrock/Vertex vs the Enterprise consumption pool |
| 2.6 | HA design, runbook, on-call rotation. This becomes a Tier-1 dependency. |

**Gate 2:**
- Does inline enforcement catch materially more than native limits did?
- Is the cache-TTL cost regression smaller than the enforcement saving?
- Is the browser/Desktop split acceptable, and can MDM realistically move the population to Desktop?
- Are you willing to own a Tier-1 service? Name the on-call owner and their backup.

---

### Phase 3 — Selective platform build (Q2+ 2027) · **conditional, and deliberately narrow**

**Only if** Gates 0–2 show a specific, high-volume, well-understood workload where owning the agent stack pays for itself.

Scope it as a **second system, not a replacement**: LibreChat for 30–50 power users — MCP orchestration against HG's own data, subagent pipelines, the data team. Never a general-population migration.

**Gate 3 — all three must be true:**
1. A named workload with measured volume where routing/self-hosting demonstrably beats native.
2. A named ≥1.0 FTE owner **and** a named backup, funded.
3. A survey confirming the target users have low dependence on Cowork, Excel/M365, mobile, and shared Artifacts.

> On current evidence, (3) is where this fails for CSMs and PMs.

---

### Phase 4 — Renegotiate (start 120 days before renewal, runs in parallel)

Independent of everything above, and possibly the highest-ROI line in this document. You are an enterprise account facing a pricing-model change Anthropic imposed mid-relationship (bundled tokens removed from Enterprise agreements from November 2025, mandatory at renewal from ~February 2026).

**This PRD is your leverage artifact.** A documented alternatives evaluation has standalone commercial value whether or not you ever build any of it. Bring: measured consumption profile, the alternatives analysis, and a credible (if unattractive) BATNA.

---

## 8. The question no gateway answers: is the spend worth it?

Goal G4 is the one that survives all of the above, and none of the tooling addresses it.

**The best available evidence cuts both ways.** ICONIQ's 2026 GTM study (n=150+ B2B software GTM leaders) found high-AI-adoption organisations generate **~2× net expansion revenue per post-sales FTE ($1.1M vs $600k)** and ~2× net-new ARR per GTM FTE. But it reports **no per-rep spend figures**, so it establishes correlation between adoption and revenue per FTE — not that $2,000/month/CSM is the efficient point on that curve. It also cannot separate adoption from the confound that better-run companies do both. Do not let it be quoted at you as blanket justification. ([ICONIQ](https://www.iconiq.com/growth/reports/gtm-org-structure-ai-2026))

**Make it your own proof instead.** ICONIQ's denominator — *net expansion revenue per post-sales FTE* — is computable inside HG this quarter. Split CSMs into high-spend and low-spend cohorts and compare NRR/expansion per head.

- If the $2,000/month CSMs are visibly above the $200/month CSMs, **you have a scaling story, not a cost problem**, and the correct action is to raise the floor rather than lower the ceiling.
- If they are not, you have your answer, and Phase 1 becomes urgent rather than prudent.

For context on how rare a real answer is: Battery Ventures found only **6%** of enterprises have a well-defined, consistent AI ROI framework, while **62% already report AI ROI to their board.** Being able to answer this credibly is a genuine differentiator, and it costs an analyst-week — not a platform.

---

## 9. Success metrics

| Metric | Baseline | Target | By |
|---|---|---|---|
| Monthly Claude run-rate | $40,000 | ≤$28,000 | +90 days |
| % spend attributable to a named user *and* task category | ~0% | ≥90% | +30 days |
| % of inference on Sonnet or below | unknown | ≥70% | +90 days |
| Users above soft cap without approval | unknown | 0 | +60 days |
| WAU/MAU (adoption guardrail) | baseline | **no decline** | ongoing |
| Self-reported output-quality degradation | — | **no credible reports** | ongoing |
| Forecast accuracy vs actual | unknown | ±10% | +120 days |

The last two are guardrails, not targets. **A 40% cost reduction that damages CSM effectiveness is a failure**, and the whole exercise is worthless if you can't tell the difference.

---

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Cost controls degrade CSM/PM output quality and the revenue effect exceeds the saving | **High** | Adoption + quality guardrails in §9; soft caps with approval paths; cohort analysis in §8 before hard limits |
| Compliance API access reads as surveillance; trust and adoption collapse | **High** | Legal/People sign-off before first pull; aggregate reporting only; classify-then-discard; communicate before, not after |
| Contract is seat-based, so cost endpoints are unavailable | Medium | **Verify in week 1.** Fall back to admin console + Compliance API session usage. |
| Model-mix saving (A2) doesn't materialise because default is already Sonnet | Medium | Phase 0 tests this before any commitment. Business case is explicitly gated on it. |
| Gateway becomes a Tier-1 single point of failure | Medium | Gate 2 requires a named on-call owner; HA design before rollout |
| Cache-TTL regression through the gateway offsets enforcement savings | Medium | Explicit measurement task (2.4) before scaling the pilot |
| Building a platform for a problem that native controls solved | Medium | The entire gate structure exists to prevent this |
| Anthropic ships native features that obsolete the build mid-flight | Medium | They shipped analytics, spend controls, and a gateway in the last two quarters. Re-check the roadmap at every gate. |
| LibreChat feature-gating post-ClickHouse acquisition | Low (if Option D deferred) | Deferred by design |

---

## 11. Open questions

1. **Which Enterprise contract are we on — usage-based or seat-based?** Blocks Phase 0. *(Finance, week 1)*
2. **Exact seat count and renewal date?** The cost model needs the real denominator, and Phase 4 needs the date. *(Finance, week 1)*
3. **Does AI usage appear in any leaderboard, OKR, enablement target, or review criterion?** *(Francis, week 1)*
4. What is the current org default model, and were Opus/Fable restrictions ever configured?
5. What's the Desktop vs browser split among high spenders? Determines whether Phase 2 is even applicable.
6. How much of the $40k is Claude Code vs Chat vs Cowork? Changes which lever matters.
7. Who owns this — RevOps, IT, Data, or Francis directly? The Battery data says at HG's scale it sits under existing tech/finance leadership rather than a new role.
8. Is there appetite to reduce seat count, or is this purely a consumption question?

---

## 12. Recommendation

**Approve Phase 0 and Phase 1 now. Defer Phases 2 and 3 pending their gates. Start Phase 4 immediately.**

Combined ask for Phases 0+1: **~0.5 FTE for 8 weeks and ~$40k**, against a modelled **~$200k/year** saving with no capability loss and no new operational surface.

The instinct behind the original question — that you are flying blind on a large and growing spend — is correct and the concern is proportionate. The proposed solution is aimed at the wrong 12% of the bill. **Do the cheap thing first, and let the data decide whether the expensive thing is warranted.**

---

## Sources

**Anthropic primary**
[Claude Enterprise pricing](https://claude.com/solutions/enterprise) · [Enterprise consumption guide](https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide) · [Analytics APIs](https://platform.claude.com/docs/en/manage-claude/analytics-api) · [Compliance API](https://platform.claude.com/docs/en/manage-claude/compliance-api) · [Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api) · [Model pricing](https://platform.claude.com/docs/en/about-claude/pricing) · [Claude apps gateway](https://code.claude.com/docs/en/claude-apps-gateway) · [Admin visibility and spend controls](https://claude.com/blog/giving-admins-more-visibility-and-control-over-claude-usage-and-spend)

**Benchmarks and case studies**
[Atlanta Fed — firm AI spending, May 2026](https://www.atlantafed.org/research-and-data/publications/policy-hub-macroblog/2026/05/06/how-much-firms-spending-on-ai-and-what-will-happen-to-headcounts) · [Ramp AI Index, Aug 2026](https://ramp.com/data/ai-index-august-2026) · [Ramp token cost benchmarks](https://ramp.com/blog/ai-token-cost-for-businesses) · [Fortune — Uber's AI budget](https://fortune.com/2026/05/26/uber-coo-ai-spending-tokens-claude-code/) · [TechCrunch — Uber caps AI spending](https://techcrunch.com/2026/06/02/uber-caps-employee-ai-spending-after-blowing-through-budget-in-four-months/) · [Microsoft employee AI spend](https://finance.yahoo.com/technology/ai/articles/microsoft-cracks-down-employee-ai-174500002.html) · [ICONIQ — GTM Org Structure in the AI Era 2026](https://www.iconiq.com/growth/reports/gtm-org-structure-ai-2026) · [Battery Ventures — State of Enterprise Tech Spending, Jun 2026](https://fr.battery.com/wp-content/uploads/2026/06/Battery_State_of_Enterprise_Tech_Spending_June_2026.pdf) · [McKinsey — Recalibrating technology budgets for the AI era](https://www.mckinsey.com/capabilities/mckinsey-technology/our-insights/recalibrating-technology-budgets-for-the-ai-era)

**FinOps**
[State of FinOps 2026](https://data.finops.org/) · [FinOps for AI Overview](https://www.finops.org/wg/finops-for-ai-overview/) · [Building a GenAI Cost and Usage Tracker](https://www.finops.org/wg/how-to-build-a-generative-ai-cost-and-usage-tracker/)

**Gateways and self-hosted UI**
[LiteLLM March 2026 security incident](https://docs.litellm.ai/blog/security-update-march-2026) · [Datadog Security Labs — LiteLLM supply-chain compromise](https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-teampcp-supply-chain-campaign/) · [CVE-2026-59822](https://www.ionix.io/threat-center/cve-2026-59822/) · [Cloudflare identity-aware AI Gateway](https://blog.cloudflare.com/identity-aware-ai-gateway/) · [Palo Alto completes Portkey acquisition](https://www.paloaltonetworks.com/company/press/2026/palo-alto-networks-completes-acquisition-of-portkey-to-secure-ai-agents) · [ClickHouse acquires LibreChat](https://clickhouse.com/blog/clickhouse-acquires-librechat) · [LibreChat Skills](https://www.librechat.ai/docs/features/skills) · [Open WebUI licence](https://docs.openwebui.com/license/) · [Open WebUI migration failure #24129](https://github.com/open-webui/open-webui/issues/24129) · [RouteLLM (LMSYS)](https://www.lmsys.org/blog/2024-07-01-routellm/) · [Semantic cache hit-rate analysis](https://dev.to/gauravdagde/llm-semantic-caching-the-95-hit-rate-myth-and-what-production-data-actually-shows-8ga)