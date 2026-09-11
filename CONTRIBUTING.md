# Contributing

Thanks for your interest. This is a small, deliberately dependency-free toolkit,
so the bar for new code is "does it earn its complexity".

## Ground rules that are not negotiable

This tool reads employees' real conversations. Three rules exist to keep it from
becoming a surveillance product, and a PR that breaks one will be declined
regardless of how good the code is:

1. **No transcript warehousing.** Classify, aggregate, discard. Do not add
   caching of transcript content to disk, a local store, or an index.
2. **Minimise what leaves the machine.** The classifier sends only the first and
   last user turn per chat. Don't widen that to full transcripts.
3. **Aggregate output.** Don't add features whose output is a readable dump of
   one named person's conversations.

Analysis output (`out/`, `profiles/`, `findings/`) is gitignored. Before you push,
run `git status` and confirm nothing outside those directories carries employee
names, verbatim prompts, customer names, or per-person cost. The gitignore
protects those *directories*, not the content *pattern*.

Never commit a real email address, even in a test. Use `someone@example.com`.

## Running the tests

```bash
python3 test_reconstruction.py
```

32 checks, fully offline, no credentials and no network. They pin the cost
reconstruction against synthetic transcripts with known properties.

**If you change anything in the cost model, the tests must still pass**, and if
you believe a test is wrong, say why in the PR rather than adjusting the expected
number to match new output. Those numbers are the spec.

## On "fixing" the reconstruction

Per-conversation cost is *reconstructed from transcript shape*, not read from an
API — the Compliance API returns no token counts and no cost anywhere. The
reconstruction assumes a human re-paying for context each turn, so it overstates
long agentic tool loops by up to 19x when the prompt cache stays warm.

The tools already detect this and **reject their own estimate** past a 40%
divergence from the Analytics actuals. That rejection is correct behaviour, not a
bug. Please don't send a PR that tunes constants until the divergence warning
stops firing; if reconstruction and actuals disagree, the actuals are right.

Improving the model itself is very welcome — a cache-aware reconstruction that
survives the divergence check on agentic sessions would be a real contribution.

## Style

- Python 3.9+, standard library only. Adding a dependency needs a reason in the PR.
- Match the surrounding code; it favours short functions and plain data over
  classes.
- Keep every signal traceable to one of the hypotheses in the README. A metric
  that doesn't discriminate between them is noise.

## Scope

Good PRs: broader surface coverage, better token estimation, a cache-aware cost
model, clearer flags, bug fixes with a test.

Out of scope: dashboards over individuals, ranking employees, anything that reads
as monitoring rather than cost diagnosis.
