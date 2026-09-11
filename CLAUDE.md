# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this project is

A root-cause investigation toolkit for Claude Enterprise spend. It answers *why*
a bill is high, not just *who* spends — and whether each workload is on the right
model and the right surface. It is written to work against **any** Enterprise
org, not one specific company: keep it that way.

Python 3.9+, **standard library only**. Adding a dependency needs a real reason.

## Actual layout

```
investigate_user.py       single-user diagnosis. Holds the shared machinery:
                          Http client, load_env, pagination (page_walk /
                          cursor_walk), analyze_turns, pricing, flags,
                          counterfactuals, hypothesis verdicts, rendering.
cohort.py                 rank a group of users, assign pattern verdicts
analyze_user.py           one-shot structured JSON audit for unattended batches;
                          imports its primitives from investigate_user
test_reconstruction.py    32 offline checks, no credentials, no network
```

`investigate_user.py` is the library as well as a CLI — the other two import from
it. Put shared logic there rather than duplicating it.

## Commands

```bash
python3 investigate_user.py someone@example.com --days 30              # metadata only
python3 investigate_user.py someone@example.com --days 30 --transcripts # tier 2
python3 cohort.py --days 30 --file cohort.txt
python3 analyze_user.py someone@example.com --days 30 --out out/audits
python3 test_reconstruction.py                                          # offline, 32 checks
```

Credentials come from `.env` (gitignored): `ANTHROPIC_COMPLIANCE_KEY`,
`ANTHROPIC_ANALYTICS_KEY`, `ANTHROPIC_API_KEY` (classification only). The
compliance key is Primary-Owner-only; an Admin key reaches `/activities` and
nothing else. A plain workspace key fails Analytics with a misleading
`401 "x-api-key header is required"` — that is a *scope* problem, not a header
bug, and the code already falls back to the compliance key.

## The central architectural constraint

Two APIs, and neither alone answers the question:

- **Enterprise Analytics** — USD cost and token counts, **no content**.
- **Compliance** — full transcripts, **no token counts and no cost anywhere**.

They join only on `(user, date)`. So cost is **reconstructed from transcript
shape** and reconciled against Analytics actuals. This is load-bearing: it is
what lets the toolkit explain *why* a conversation was expensive. Any change that
breaks the reconstruction breaks the product.

```
billed_input = Σ over assistant turns i of ( overhead + Σ tokens of turns before i )
```

Cost is quadratic in conversation length, not linear. `repayment_multiplier`,
`project_overhead_tokens` and `n_cold_resumes` all fall out of this formula.
README §"The hypothesis tree" lists H1–H6; keep signals traceable to a hypothesis
rather than adding metrics that don't discriminate between them.

### Do not "fix" the divergence rejection

The reconstruction assumes a human re-paying for context each turn. In a long
agentic tool loop the prefix stays warm, so re-payment bills at the cache-read
rate — the model overstates these by up to **19×**. The tools detect this,
**reject their own estimate past 40% divergence**, and fall back to reporting
lever weights as shares rather than dollars. That is correct behaviour.

When reconstruction and Analytics disagree, **Analytics is right**. Never tune
constants until the warning stops firing. The *shape* findings (turn counts, tool
calls, model mix) stay valid either way — they are measured, not reconstructed.

## Tiers are a privacy boundary, not a convenience

Each tier reads strictly more sensitive data than the last, and users should
**stop at the lowest tier that answers their question**. Tier 2 (`--transcripts`)
reads employees' actual conversations and requires Legal/People sign-off.

When touching tier-2 code, preserve these:

- The classifier sends only the **first and last user turn** per chat, never full
  transcripts.
- **Classify then discard** — no transcript warehousing or caching to disk.
- Report in aggregate; don't add features that circulate individual transcripts.

## Keeping it publishable

This repo is intended to be open-sourceable, so:

- **No org-specific hardcoding.** Internal MCP tool names go in
  `retrieval_tools.txt` (gitignored), not in source. `analyze_user.py` has
  built-in defaults plus a read-only-verb prefix heuristic.
- **Never commit a real email address**, even in an example or test — use
  `someone@example.com`.
- Analysis output (`out/`, `profiles/`, `findings/`) is gitignored. The gitignore
  protects those *directories*, not the content *pattern* — check `git status`
  for files elsewhere carrying names, prompts, customer names or per-person cost.

## Implementation gotchas

- **Pagination differs by endpoint family.** Chats use `after_id`/`has_more`
  cursors (`cursor_walk`); sessions and projects use `page`/`next_page`
  (`page_walk`).
- **Rate limit is 600 req/min** on the Compliance API; back off accordingly.
- **Cost data lags.** Analytics refreshes ~every 4h with up to 24h delay and is
  revised for 30 days — windows end **2 days back** to avoid provisional numbers.
- **Token counting is a ~3.9 chars/token heuristic**, deliberately. Good enough
  to rank users and size 10–50× effects.
- **Transcripts are partly redacted:** thinking blocks, system prompts, tool
  definitions, MCP config, images/PDFs and citation metadata don't come through.
  Text, tool calls and tool results do.
- **Coverage gaps:** Claude Code via Bedrock/Vertex/Foundry, Claude Code on the
  web, and Console-API-key sessions are absent from Compliance session data. ZDR
  and HIPAA-readiness orgs capture nothing.
- Tier 0 requires a **usage-based** Enterprise contract; seat-based legacy
  contracts expose usage credits only.

## Writing style

Docs here are analyst prose: claims are sourced or explicitly sized, uncertainty
is stated rather than smoothed over (e.g. caching is "2.8× measured, not the
quoted 10×"), and the counterfactual cost of each fix is named. Lead with the
number, then the caveat.
