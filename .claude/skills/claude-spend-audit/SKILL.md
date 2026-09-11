---
name: claude-spend-audit
description: Audit one user's Claude spend and produce a written profile. Use when asked to analyse, audit, or investigate a named user's Claude usage or cost, or to work through a batch of top spenders. Answers not just "how much" but "is this the right compute type and the right model tier for the work".
---

# Claude spend audit — one user

Produces a per-user profile matching the six already in `profiles/`.

## Run this first

```bash
python3 analyze_user.py <email> --days 30 --max-sessions 14 --scan-sessions 60
```

Writes `out/audits/<user>.json` and prints a one-line summary. Takes 3–10
minutes depending on session count. **Do not re-implement this in Python
yourself** — the script handles auth fallback, pagination (two different
schemes), rate limits, and the token accounting.

If it fails: `ANTHROPIC_ANALYTICS_KEY` in `.env` is usually a plain workspace
key that cannot read the Analytics endpoints. The script already falls back to
the compliance key. A 401 saying `x-api-key header is required` is a *scope*
problem, not a header bug.

## Then write `profiles/<firstname>-<lastname>.md`

Read the JSON and follow the structure of an existing profile
(any existing file in `profiles/` is a good template). Required sections:

1. **Verdict** — one paragraph. Which of the patterns below, and why.
2. **Measured** — table from the JSON. Facts only.
3. **Interpretation** — clearly labelled as inference.
4. **Recommended actions** — ranked, each naming an axis (see below).
5. **What I do not know** — mandatory, never omit.
6. **Confidence** — separately for mechanism vs dollars vs value.

## The two axes

Judge every workload on both:

|  | Right model tier | Wrong model tier |
|---|---|---|
| **Needs a model** | correct | over-provisioned intelligence (~5x cap) |
| **Should be code** | wrong compute type (→0) | worst quadrant |

**Model tier saves at most ~5x. Moving work to code takes marginal cost to ~0.**
Weight recommendations accordingly.

But note the refinement from `FINDINGS-determinism.md`: most waste is neither
— it is **the same correct model work repeated with no memory**. Check
`determinism.duplicate_retrieval_rate` before recommending any rewrite.

## Flags the script emits, and what each means

| Flag | Meaning | Usual fix |
|---|---|---|
| `NO_MEMOISATION` | >50% of retrieval calls are duplicates | fix the looping skill; TTL-cache our own MCP servers. **Not** a generic platform cache — hooks cannot return cached results and ~91% of duplicates are built-in tools |
| `FAT_PROJECT` | project attaches >100k tokens per turn | split large files out of the project |
| `AGENTIC` | >70% spend on Cowork/Code | judge model choice, not turn count |
| `PREMIUM_HEAVY` | >60% spend on Opus/Fable | candidate for tier downgrade |
| `NO_HUMAN_IN_LOOP` | median inter-turn gap ≤2s | agent loop; "split your threads" advice is meaningless |
| `UNATTENDED_SCHEDULES` | ≥5 scheduled sessions | check cadence and whether output is read |
| `HUGE_CONTEXT` | >200k input tokens/request | usually a fat project or bulk data as context |
| `HIGH_ERROR_RATE` | >3% tool errors | silent retry cost |
| `BROWSER_DRIVING` | >15% browser/screenshot tools | an API almost certainly exists |
| `SLEEP_POLLING` | `bash sleep` in transcripts | paying inference rates to wait |

## Known patterns to match against

From the first six users (`FINDINGS-determinism.md`):

- **No memoisation** (universal) — same query/file/ticket fetched dozens of
  times. Check whether the repeats are built-in tools (`Read`, `Bash`,
  `web_fetch`, `Grep`) or MCP: only MCP ones are cacheable, the rest need the
  skill fixed.
- **Browser-driving an app that has an API** — e.g. editing Slides via
  screenshots, or triaging email through a browser, on a premium model.
  Seen 3x in 24 users.
- **Bulk data as chat context** — MBs of CSV re-sent every turn; belongs on
  disk + pandas (Claude Code), or in BI if the report recurs.
- **Poll-with-sleep** — `bash sleep` in a loop; a workflow-engine problem
  solved with an LLM.
- **Duplicate scheduled jobs** — e.g. two near-identical nightly email checks.
- **Premium tier on routine work** — summarisation and triage on Opus/Fable.
- **The same job invented independently** — three people built email triage on three models.

If a user matches a known pattern, say so and cite the precedent rather than
re-deriving it.

## Rules

- **Dollars come from the Analytics API only.** Per-conversation cost
  reconstruction overstates agentic sessions by up to 19x (warm cache). Never
  quote a reconstructed per-chat dollar figure.
- **Separate measurement from inference.** The JSON is fact; why they do it is
  a hypothesis. Label it.
- **Never conclude a workload is wasteful because it is large.** The most
  tool-heavy user audited ran 93,642 tool calls for $694 — efficiently, on
  Sonnet. Cost per request and duplicate rate matter far more than volume.
- **You cannot see value.** Telemetry shows cost, never whether the output was
  read or useful. Say so, and recommend asking the user.
- **Do not recommend rewrites for non-engineers** without saying who would
  build it. One marketer authors sophisticated ClickHouse SQL through the
  model precisely because he cannot write it himself — that is correct use.
- This reads employees' actual conversations. Report in aggregate; quote
  prompts sparingly and only where they carry the finding.

## Batch mode

For several users, run `analyze_user.py` for each (they are independent and
can run in parallel), then write one profile each, then update
`profiles/README.md` with the new rows and any cross-cutting pattern.
