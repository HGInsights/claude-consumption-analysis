## What this changes

<!-- One or two sentences. -->

## Why

<!-- Which hypothesis (H1-H6) or gap does this serve? -->

## Checklist

- [ ] `python3 test_reconstruction.py` passes (32 checks, offline)
- [ ] No new dependencies, or the PR explains why one is needed
- [ ] No employee names, real email addresses, customer names, verbatim prompts,
      or per-person cost figures anywhere in the diff — including test fixtures
      and example output
- [ ] `git status` is clean outside `out/`, `profiles/`, `findings/`
- [ ] No transcript content is cached or written to disk
- [ ] If the cost model changed: the reconciliation check still rejects
      estimates past 40% divergence, and expected test numbers were not edited
      merely to match new output
