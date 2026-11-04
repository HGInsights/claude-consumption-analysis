---
name: Bug report
about: Something the toolkit does wrong
labels: bug
---

<!--
Do not paste real transcripts, email addresses, API keys, customer names or
per-person cost figures here, even redacted ones. Use someone@example.com and
round or synthetic numbers.

Security or privacy-boundary problems (the tool reads, keeps or sends more than
it should) go through the Security tab, not here. See SECURITY.md.
-->

## What happened

## What you expected

## How to reproduce

Command run (with emails replaced by `someone@example.com`):

```
python3 ...
```

## Environment

- Python version:
- Script and tier (e.g. `investigate_user.py`, tier 0 / with `--transcripts`):
- Contract type, if relevant (usage-based / seat-based):
- Does `python3 test_reconstruction.py` pass? (yes / no)

## If it's about cost numbers

Did the reconciliation reject the estimate (the >40% divergence warning)? That
rejection is expected on agentic sessions and isn't a bug on its own; see
CONTRIBUTING.md.
