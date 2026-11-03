# Security policy

## What counts as a security issue here

This toolkit reads an organisation's Claude Enterprise data with Primary-Owner
credentials. At tier 2 that includes employees' actual conversations. So there
are two kinds of report we treat as security issues, and we treat them as equally
important:

1. **Code vulnerabilities.** The usual kinds: credential leakage (keys written
   to logs, output files or error messages), unsafe handling of API responses,
   and anything that lets a crafted transcript or API response execute code or
   write outside the output directory.
2. **Privacy-boundary failures.** Places where the tool reads, keeps or exposes
   more than it says it does, even if nothing is technically "exploited". For
   example:
   - transcript content written to disk, cached or retained beyond a run
   - the classifier sending more than the first and last user turn per chat
   - a lower tier (`investigate_user.py` without `--transcripts`, or
     `org_scan.py` without `--transcripts`/`--deep`) fetching transcript content
   - output that is a readable dump of one named person's conversations
   - analysis output written outside the gitignored `out/`, `profiles/` and
     `findings/` directories by default

The second category matters as much as the first. A bug that turns this into a
surveillance tool is a security bug.

## Reporting

Please **do not open a public issue** for either kind. Report privately through
this repository's **Security** tab → **Report a vulnerability**. Only the
maintainers see the report.

Include what you ran, what you expected the tool to touch, and what it actually
touched. **Do not attach real transcripts, email addresses, API keys or
per-person cost figures**, even your own. A synthetic reproduction is enough;
`test_reconstruction.py` shows how to build one.

We aim to acknowledge reports within 5 working days. This is a small project
maintained on a best-effort basis, so we can't promise more than that.

## Misuse

If you believe someone is using this toolkit to monitor employees without their
knowledge or without the sign-off the README asks for, that is a matter for
that organisation's Legal, HR or works council, not for this repository. We
can't act on it, but we do want to hear about designs that make such misuse
easier. Report those through the same private channel.

## Supported versions

Only the latest commit on `main` is supported.

## Scope

Out of scope: vulnerabilities in Anthropic's APIs or in Claude itself. Report
those to Anthropic directly. This project is not affiliated with Anthropic.
