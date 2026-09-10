# claude-consumption-analysis

Tooling to investigate HG Insights' Claude Enterprise spend: who is spending,
on which model, through which surface — and whether the work needs a model at
all.

## ⚠️ This repo does not track analysis output

`profiles/`, `findings/` and `out/` are gitignored and must stay that way.
They contain employee names, verbatim excerpts from employees' actual
conversations, named customers and job candidates, and per-person cost data.

**Before every commit, check `git status` for files outside those folders that
carry the same content.** The gitignore protects the directories, not the
content pattern.

Analysis output lives on disk and is shared deliberately, not through git.

---

# Root-cause investigation: where the $40k/month actually goes

A working toolkit, not a plan. Point it at your org and it tells you which
behaviours are burning the money, ranked by dollars, with the counterfactual
cost of each fix.

---

## 1. The Compliance API, since you haven't seen it

It's an Enterprise-only API that exposes your org's actual Claude content for
compliance, eDiscovery, and DLP. It is not the usage API and it is not the
admin console — it's a separate key with separate scopes.

**Getting a key.** claude.ai → Organization settings → Compliance access key.
Primary Owner only. An Admin API key does *not* work for most of this — it
reaches the activity feed and nothing else. Content and session endpoints need
`read:compliance_user_data`.

**What you can pull** (`https://api.anthropic.com/v1/compliance/…`):

| Endpoint | Gives you |
|---|---|
| `GET /activities` | Org-wide event feed: actor email, user ID, IP, user agent, event type (`claude_chat_created`, …), resource IDs |
| `GET /apps/chats` | Every claude.ai chat: id, title, **model**, project_id, user, created/updated, direct `href` |
| `GET /apps/chats/{id}/messages` | Full transcript — every user and assistant turn, attached files, generated files, artifacts |
| `GET /apps/projects` + `/{id}/attachments` | Projects and everything attached to them, with `size_bytes` |
| `GET /apps/sessions/local` + `/{id}/messages` | Cowork, Claude Code, Claude Science, Office agents — transcripts including every tool call and result |
| `GET /apps/sessions/remote` | Cowork on web/mobile |
| `DELETE …` | Content deletion, with `delete:compliance_user_data` |

Rate limit 600 req/min. Retention 6 years by default. Pagination differs by
endpoint family — chats use `after_id`/`has_more` cursors, sessions and
projects use `page`/`next_page`. The clients here handle both.

**Redacted from transcripts:** thinking blocks, the system prompt, tool
definitions and MCP config, images/PDFs/binary blocks, citation metadata.
Text, tool calls, and tool results come through.

### ⚠️ The thing that changes the approach

**The Compliance API returns no token counts and no cost. Anywhere.** Both the
chat and session docs state it explicitly. Cost lives only in the Enterprise
Analytics API (`/v1/organizations/analytics/user_cost_report`), which has no
content.

So the investigation is a **join across two APIs on (user, date)** — and,
more usefully, a **reconstruction of cost from transcript shape**, which is
what this toolkit does. That turns out to be the more powerful move, because
it tells you *why* a chat was expensive, which the cost API never can.

---

## 2. The hypothesis tree

Six candidate root causes. Each has a signal that confirms or refutes it, and
each has a different fix — which is the point of separating them.

### H1 — Context re-payment (the quadratic) · **most likely**

Cost is not proportional to conversation length. It's proportional to the
**sum of context at every turn**, which grows quadratically:

```
billed_input  =  Σ over assistant turns i of ( overhead + Σ tokens of turns before i )
```

A 40-turn chat re-pays for its early messages ~20 times. **Verified against
the fixture:** two users sending byte-identical content, one in a single
40-turn thread and one in five 8-turn threads, differ **5.0× on input tokens**
and **3.5× on blended cost**. Neither user did anything visibly wrong.

- **Signal:** `repayment_multiplier` = billed_input ÷ content_tokens. Healthy is under 8. Alice's was 20.
- **Confirms:** median assistant turns per chat > 20; multiplier > 15.
- **Refutes:** short threads with high cost — then look at H2 or H3.
- **Fix:** enablement, not technology. "Start a new chat when the topic changes."

### H2 — Fat projects

Every file attached to a Project is re-sent as context on **every message of
every chat in that project**. A project with 300k tokens of attachments costs
**$1.50 per turn on Opus before anyone types anything** — $30 across a 20-turn
chat, verified in the fixture.

- **Signal:** `project_overhead_tokens`, and `tier1_projects.csv` ranks every project by attachment weight.
- **Fix:** trim attachments. Modelled recovery: ~80% of the overhead.
- **Why this is my second bet for CSMs:** they're exactly the population that attaches every account doc to a shared project and never prunes it.

### H3 — Premium model on trivial work

Fable/Mythos are **5×** Sonnet. Opus is **2.5×**. Ramp's data shows premium
models went from 5.7% to 55.9% of business AI cost in ten months, so the
default drifting upward is the market norm, not an HG quirk.

- **Signal:** `pct_premium_model` per user (tier0, no content needed); `PREMIUM_ON_TRIVIAL` flag.
- **Fix:** org default model + role entitlements. Free, reversible, same-day.

### H4 — Cold cache

Prompt caching has a 5-minute TTL. The gap that kills it is **human think
time** — previous response to next question. A user who asks something, reads
it, thinks for ten minutes, then follows up pays full input price for the
entire conversation, every single turn.

- **Signal:** `n_cold_resumes` / `n_assistant_turns`. The fixture confirms 45-minute gaps go cold on 19 of 20 turns.
- **Honest sizing:** caching is quoted as "10× cheaper," but that's the cached prefix alone. The newest turn pays full price and **output is never cacheable**. Measured blended saving in the fixture: **2.8×**, not 10×.
- **Fix:** mostly not actionable by the user. Useful as an explanation, not a lever.

### H5 — Agentic fan-out

Cowork and Claude Code generate large intermediate token volumes — tool calls,
file reads, subagents. A CSM running Cowork over a folder of account docs can
out-spend an engineer.

- **Signal:** tier0 `top_product`; then `sessions/local` transcripts and tool-call counts.
- **Fix:** surface gating by role.

### H6 — The spend is legitimate

Must stay on the list. If the top spenders are doing hard, valuable work on
appropriate models in well-shaped threads, **the finding is that $40k is the
price** and the action is renegotiation, not restriction.

- **Signal:** low multipliers, few flags, high `complexity` in classification, tight model-to-task fit.
- **Test it properly:** compare net expansion revenue per head for high-spend vs low-spend CSMs.

---

## 3. Running it

Three tiers of escalating sensitivity. **Stop as soon as you have your answer.**

```bash
pip install requests

export ANTHROPIC_ANALYTICS_KEY=...     # claude.ai > Org settings > API
export ANTHROPIC_COMPLIANCE_KEY=...    # claude.ai > Compliance access key
export ANTHROPIC_API_KEY=...           # only for --classify

python run.py tier0 --days 30                      # who + which model. No content.
python run.py tier1 --days 30                      # + project weights. Titles only.
python run.py tier2 --days 30 --top 10 --classify  # + transcripts. Needs sign-off.
```

**Tier 0** — Analytics API only, ~10 minutes, no content, no review needed.
Ranks users by cost, shows premium-model share and cumulative concentration.
*This may be all you need.* If the top 10 users are 70% of spend and 80%
premium-model, H3 is your answer and you can act on Monday.

**Tier 1** — adds project attachment weights and per-turn cost. Metadata only
(titles, file sizes). Tests H2 directly.

**Tier 2** — pulls transcripts for named top spenders, computes every signal,
and optionally classifies each chat by task type and judges whether the model
tier was over-provisioned. **This reads employees' actual conversations.**

Output lands in `out/` as CSV plus a JSON summary: per-chat diagnosis with
counterfactual costs, per-user rollup, and root causes ranked by dollars
involved.

### ⚠️ Before tier 2

Get Legal and People sign-off *first*, and tell people *before* rather than
after. Concretely:

- Scope to a named investigation, not standing access.
- Report in aggregate; don't circulate individual transcripts.
- **Classify then discard.** Don't warehouse transcripts — the FinOps
  Foundation advises against retaining prompts and outputs on both security
  and cost grounds, and warehousing them turns every employee's unfiltered
  questions into discoverable records.
- The classifier defaults to sending only the **first and last user turn**
  per chat, not full transcripts. Keep it that way.
- Interview your top 5 spenders before drawing conclusions from their data.
  It's faster, it's fairer, and they'll tell you things the transcripts won't.

### One thing to check before any of it

Confirm AI usage appears in **no** leaderboard, OKR, enablement target, or
review criterion. An internal usage leaderboard is what caused Uber to burn
its entire 2026 AI budget in four months. If one exists at HG, removing it is
free and is probably worth more than everything else here.

---

## 4. What you get

`out/tier0_users.csv` — cost per user, $/month, premium share, cumulative concentration
`out/tier1_projects.csv` — every project by attachment weight and cost per turn
`out/tier2_chats.csv` — per chat: turns, tokens, re-payment multiplier, cold resumes, flags, and four counterfactual costs
`out/tier2_summary.json` — root causes ranked by dollars, per-user rollup, savings by lever

Console output ends with the two tables that matter:

```
ROOT CAUSES (by estimated cost involved)
CONTEXT_REPAYMENT        142   $  8,410
FAT_PROJECT               38   $  5,220
PREMIUM_ON_TRIVIAL       210   $  3,100

SAVINGS BY LEVER (est, over the sampled window)
split long threads       $  6,900
downgrade to Sonnet      $  4,200
trim project files       $  4,100
```

---

## 5. Caveats

- **Token estimation is a ~3.9 chars/token heuristic.** Fine for ranking users and sizing 10–50× effects; swap in the `count_tokens` API if you need precision.
- **Cost data lags.** Analytics cost refreshes every ~4h with up to 24h delay and is revised for 30 days. The runner ends its window 2 days back to avoid provisional numbers.
- **Not everything is covered.** Claude Code via Bedrock/Vertex/Foundry, Claude Code on the web, and Console-API-key sessions don't appear in Compliance session data. ZDR and HIPAA-readiness orgs capture nothing.
- **Cost/usage endpoints need a usage-based Enterprise contract.** Seat-based legacy contracts expose usage credits only. Verify this first — it blocks tier 0.
- **The diagnosis reconstructs cost; it doesn't read it.** Reconcile the tier-2 estimate against tier-0 actuals for the same users and window. If they disagree by more than ~30%, trust the Analytics API and tell me — the model needs adjusting.

## 6. Files

```
run.py                    three-tier orchestration
claudespend/clients.py    Analytics + Compliance clients, pagination, backoff
claudespend/diagnose.py   the cost-reconstruction engine and flags
claudespend/classify.py   task taxonomy + over-provisioning judgement (Haiku)
test_fixture.py           20 checks against synthetic chats with known properties
```

`python3 test_fixture.py` — runs offline, no credentials, all 20 pass.