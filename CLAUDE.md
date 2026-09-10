# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

**The code described in `readme.md` does not exist yet.** The repo currently contains only
`readme.md` (the spec for the toolkit) and `src_documents/prd.md` (the business case that
motivated it). `run.py`, `claudespend/`, and `test_fixture.py` are all still to be written.

Treat `readme.md` as the design spec: it names the modules, the CLI surface, the output files,
and the verified fixture numbers the implementation is expected to reproduce. It is written in
the past tense ("verified against the fixture", "all 20 pass") because it describes the intended
finished state — do not read those claims as evidence that code ran.

## What this project is

A root-cause investigation toolkit for HG Insights' ~$40k/month Claude Enterprise bill. It
answers *why* spend is high, not just *who* spends. `src_documents/prd.md` is the decision
document; `readme.md` is the toolkit that implements its Phase 0 (instrument before building).

## The central architectural constraint

Two Anthropic APIs, and neither alone answers the question:

- **Enterprise Analytics API** (`/v1/organizations/analytics/user_cost_report`) — has USD cost
  and token counts, has **no content**.
- **Compliance API** (`/v1/compliance/…`) — has full transcripts, and returns **no token counts
  and no cost anywhere**.

So cost is **reconstructed from transcript shape** rather than read, then reconciled against
Analytics actuals. This is the load-bearing design decision: it's what lets the toolkit explain
*why* a chat was expensive. Any change that breaks the reconstruction breaks the product.
Tier-2 estimates disagreeing with tier-0 actuals by >~30% means the cost model is wrong, not
the API.

The two APIs also join only on `(user, date)`, which is why the tier structure exists.

### Cost reconstruction, in one line

```
billed_input = Σ over assistant turns i of ( overhead + Σ tokens of turns before i )
```

Cost is quadratic in conversation length, not linear. Every headline signal
(`repayment_multiplier` = billed_input ÷ content_tokens, `project_overhead_tokens`,
`n_cold_resumes`) falls out of this formula. `readme.md` §2 lists the six hypotheses (H1–H6)
each signal is designed to confirm or refute — keep signals traceable to a hypothesis rather
than adding metrics that don't discriminate between them.

## Intended layout

```
run.py                    three-tier orchestration (CLI entry point)
claudespend/clients.py    Analytics + Compliance clients, pagination, backoff
claudespend/diagnose.py   cost-reconstruction engine and flags
claudespend/classify.py   task taxonomy + over-provisioning judgement (Haiku)
test_fixture.py           20 checks against synthetic chats with known properties
```

## Commands

```bash
pip install requests

python run.py tier0 --days 30                      # Analytics only. No content.
python run.py tier1 --days 30                      # + project weights. Titles only.
python run.py tier2 --days 30 --top 10 --classify  # + transcripts. Needs sign-off.

python3 test_fixture.py                            # offline, no credentials, 20 checks
```

Credentials come from `.env` (gitignored): `ANTHROPIC_ANALYTICS_KEY`,
`ANTHROPIC_COMPLIANCE_KEY`, `ANTHROPIC_API_KEY` (the last only for `--classify`).
The compliance key is a distinct Primary-Owner-only credential — an Admin API key reaches
`/activities` and nothing else.

Output goes to `out/` as CSV plus `tier2_summary.json`.

## Tiers are a privacy boundary, not a convenience

Each tier reads strictly more sensitive data than the last, and the design intent is that users
**stop at the lowest tier that answers their question**. Tier 2 reads employees' actual
conversations and requires Legal/People sign-off.

When touching tier-2 code, preserve these:

- The classifier sends only the **first and last user turn** per chat, never full transcripts.
- **Classify then discard** — do not add transcript warehousing or caching to disk.
- Report in aggregate; don't add features that circulate individual transcripts.

## Implementation gotchas

- **Pagination differs by endpoint family.** Chats use `after_id`/`has_more` cursors; sessions
  and projects use `page`/`next_page`. `clients.py` must handle both.
- **Rate limit is 600 req/min** on the Compliance API; back off accordingly.
- **Cost data lags.** Analytics refreshes ~every 4h with up to 24h delay and is revised for 30
  days — end the query window **2 days back** to avoid provisional numbers.
- **Token counting is a ~3.9 chars/token heuristic**, deliberately. Good enough to rank users
  and size 10–50× effects. Swap in the `count_tokens` API only if precision is genuinely needed.
- **Transcripts are partly redacted:** thinking blocks, system prompts, tool definitions, MCP
  config, images/PDFs, and citation metadata don't come through. Text, tool calls, and tool
  results do.
- **Coverage gaps:** Claude Code via Bedrock/Vertex/Foundry, Claude Code on the web, and
  Console-API-key sessions are absent from Compliance session data. ZDR and HIPAA-readiness
  orgs capture nothing.
- Tier 0 requires a **usage-based** Enterprise contract; seat-based legacy contracts expose
  usage credits only.

## Writing style

`readme.md` and `prd.md` are analyst prose aimed at executives: claims are sourced or explicitly
sized, uncertainty is stated rather than smoothed over (e.g. caching is "2.8× measured, not the
quoted 10×"), and the counterfactual cost of each fix is named. Match that register in docs —
lead with the number, then the caveat.
