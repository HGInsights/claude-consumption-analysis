#!/usr/bin/env python3
"""
Regenerate the sample output shown in README "What you get".

Every input here is invented: no API calls, no credentials, no real person.
The inputs are run through the same code paths a live investigate_user.py run
uses (analyze_turns, estimate_cost, flags_for, counterfactuals, render), so the
sample shows what the tool actually prints.

    python3 readme_sample.py           # print the full output
    python3 readme_sample.py --check   # fail if the README sample has drifted

The README shows an abridged copy. --check confirms every line in that copy
still appears in the generated output, so a change to render() that alters
the sample fails CI instead of leaving the README stale.
"""
import contextlib
import io
import os
import sys
import types
from datetime import datetime, timedelta, timezone

from investigate_user import (analyze_turns, counterfactuals, estimate_cost,
                              flags_for, render)

HERE = os.path.dirname(os.path.abspath(__file__))
OPUS, SONNET = "claude-opus-5", "claude-sonnet-5"
PROJECT_TOKENS = 140_000        # project attachments re-sent on every turn
ACTUAL = 352.80                 # invented Analytics total for the window

# (title, assistant turns, gap between turns in s, user chars, assistant chars,
#  model, in the fat project)
CHATS = [
    ("Quarterly pricing review",   46, 14 * 60, 3000, 5000, OPUS,   True),
    ("Vendor contract comparison", 38, 22 * 60, 6000, 4000, OPUS,   True),
    ("Board update draft v3",      31,  9 * 60, 2500, 6000, OPUS,   True),
    ("Pipeline notes - week 36",   24, 30 * 60, 4000, 3000, OPUS,   False),
    ("Rewrite onboarding email",    3,      60,  600, 1200, OPUS,   False),
    ("Summarise call transcript",   2,      60, 9000, 2500, OPUS,   False),
    ("Territory plan questions",   12,  4 * 60, 1500, 2500, SONNET, False),
]


def thread(n_turns, gap_s, user_chars, asst_chars, model, start):
    t, msgs = start, []
    for _ in range(n_turns):
        stamp = t.isoformat().replace("+00:00", "Z")
        msgs.append({"role": "user", "created_at": stamp,
                     "content": [{"type": "text", "text": "x" * user_chars}]})
        msgs.append({"role": "assistant", "model": model, "created_at": stamp,
                     "content": [{"type": "text", "text": "y" * asst_chars}]})
        t += timedelta(seconds=gap_s)
    return msgs


def generate():
    t0 = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
    per_chat = []
    for i, (title, n, gap, uc, ac, model, in_project) in enumerate(CHATS):
        st = analyze_turns(thread(n, gap, uc, ac, model, t0 + timedelta(days=i)))
        ptok = PROJECT_TOKENS if in_project else 0
        per_chat.append({
            "kind": "chat", "id": f"c{i}", "title": title, "model": model,
            "project_id": "p1" if in_project else None, "project_tokens": ptok,
            **st,
            "est_cost": estimate_cost(st, model, ptok),
            "flags": flags_for(st, ptok, model),
            "counterfactuals": counterfactuals(st, model, ptok),
        })

    # Analytics-side aggregates, invented but mutually consistent: ~110k input
    # tokens per request and a low cache-hit rate, matching the chats above.
    by_model = {OPUS: ACTUAL * 0.83, SONNET: ACTUAL * 0.17}
    by_product = {"chat": ACTUAL}
    by_context = {"0-200k": ACTUAL * 0.94, "200k-1M": ACTUAL * 0.06}
    usage = {"uncached_input": 19_200_000, "cache_read": 5_600_000,
             "cache_write": 1_100_000, "output": 210_000, "requests": 236}
    args = types.SimpleNamespace(days=30, transcripts=True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        render(args, None, None, ACTUAL, usage, {}, by_model, by_product, {},
               by_context, [{}] * len(CHATS), [], [], [], {},
               {"p1": {"est_tokens": PROJECT_TOKENS}}, per_chat, None, None, None)
    return buf.getvalue()


def readme_sample(path):
    """Lines of the first code block under '## What you get'."""
    text = open(path, encoding="utf-8").read()
    section = text.split("## What you get", 1)[1]
    block = section.split("```", 2)[1]
    return block.splitlines()[1:]          # drop the language tag line


def check(output, readme=os.path.join(HERE, "README.md")):
    produced = set(output.splitlines())
    stale = [line for line in readme_sample(readme)
             if line.strip() and line != "…" and not line.startswith("$ ")
             and line not in produced]
    if stale:
        print("README sample has drifted from render() output. Lines not produced:")
        for line in stale:
            print(f"  {line!r}")
        print("Regenerate with: python3 readme_sample.py")
        return 1
    print("README sample matches render() output.")
    return 0


if __name__ == "__main__":
    out = generate()
    if "--check" in sys.argv[1:]:
        sys.exit(check(out))
    print(out, end="")
