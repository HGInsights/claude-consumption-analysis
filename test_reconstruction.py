#!/usr/bin/env python3
"""
Offline checks on the cost-reconstruction engine. No credentials, no network.
Reproduces the fixture numbers quoted in readme.md.

    python3 test_reconstruction.py
"""
import sys
from datetime import datetime, timedelta, timezone
from investigate_user import (analyze_turns, estimate_cost, price_for, toks,
                              flags_for, counterfactuals, is_premium, text_len)

PASS = FAIL = 0

def check(name, got, want, tol=0.15):
    global PASS, FAIL
    ok = (abs(got - want) <= abs(want) * tol) if isinstance(want, float) else (got == want)
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<52} got={got} want={want}")
    if ok: PASS += 1
    else:  FAIL += 1

def thread(n_turns, gap_s, chars=4000, model="claude-opus-5"):
    t = datetime(2026, 9, 1, tzinfo=timezone.utc); msgs = []
    for _ in range(n_turns):
        stamp = t.isoformat().replace("+00:00", "Z")
        msgs.append({"role": "user", "created_at": stamp,
                     "content": [{"type": "text", "text": "x" * chars}]})
        msgs.append({"role": "assistant", "model": model, "created_at": stamp,
                     "content": [{"type": "text", "text": "y" * chars}]})
        t += timedelta(seconds=gap_s)
    return msgs

COLD = 45 * 60   # human think time: kills the 5-minute cache
WARM = 60

print("\nH1 — context re-payment (the quadratic)")
long_ = analyze_turns(thread(40, COLD))
short = [analyze_turns(thread(8, COLD)) for _ in range(5)]
sb = sum(s["billed_input_tokens"] for s in short)
check("40-turn vs 5x8-turn, input tokens", round(long_["billed_input_tokens"] / sb, 1), 5.0)
check("40-turn repayment multiplier", long_["repayment_multiplier"], 20.0)
check("8-turn repayment multiplier (healthy <8)", short[0]["repayment_multiplier"], 4.0)
lc = estimate_cost(long_, "claude-opus-5", 0)
sc = sum(estimate_cost(s, "claude-opus-5", 0) for s in short)
check("40-turn vs 5x8-turn, blended cost", round(lc / sc, 1), 3.5)

print("\nH4 — cold cache (gap is prev RESPONSE -> next question)")
cold = analyze_turns(thread(20, COLD))
warm = analyze_turns(thread(20, WARM))
check("45-min gaps go cold", cold["cold_resumes"], 20)
check("60-sec gaps stay warm (turn 1 always cold)", warm["cold_resumes"], 1)
cc = estimate_cost(cold, "claude-opus-5", 0)
wc = estimate_cost(warm, "claude-opus-5", 0)
check("blended saving from warm cache (~2.8x, not 10x)", round(cc / wc, 1), 3.5)

print("\nH2 — fat projects")
st = analyze_turns(thread(20, COLD))
base = estimate_cost(st, "claude-opus-5", 0)
# readme quotes ~$1.50/turn on Opus, which corresponds to a ~100k-token
# project (100k x $15/MTok = $1.50), not the 300k figure in its prose.
fat = estimate_cost(st, "claude-opus-5", 100_000)
check("100k-token project, $/turn on Opus (cold)", round((fat - base) / 20, 2), 1.50, tol=0.1)
big = estimate_cost(st, "claude-opus-5", 300_000)
check("300k-token project, $/turn on Opus (cold)", round((big - base) / 20, 2), 4.50, tol=0.1)
check("300k project over a 20-turn chat (readme: ~$30 at 100k)",
      round(big - base, 0), 90.0, tol=0.1)
warm_st = analyze_turns(thread(20, WARM))
wbase = estimate_cost(warm_st, "claude-opus-5", 0)
wfat = estimate_cost(warm_st, "claude-opus-5", 300_000)
# 19 of 20 turns warm -> project served from cache at 10% of list.
check("same project is far cheaper when the cache stays warm",
      round(((big - base) / 20) / ((wfat - wbase) / 20), 1), 6.9, tol=0.1)

print("\nH3 — model pricing")
check("Opus input $/MTok", price_for("claude-opus-5")[0], 15.0)
check("Sonnet input $/MTok", price_for("claude-sonnet-5")[0], 3.0)
check("Opus is 5x Sonnet", price_for("claude-opus-5")[0] / price_for("claude-sonnet-5")[0], 5.0)
check("unknown model falls back to Sonnet-class", price_for("some-new-model")[0], 3.0)
check("opus flagged premium", is_premium("claude-opus-5"), True)
check("sonnet not premium", is_premium("claude-sonnet-5"), False)

print("\nToken + content accounting")
check("chars->tokens heuristic (3.9)", toks(3900), 1000)
check("tool_use input counted", text_len([{"type": "tool_use", "name": "R",
      "input": '{"file_path":"a.py"}'}]) > 20, True)
check("tool_result text counted", text_len([{"type": "tool_result",
      "content": [{"type": "text", "text": "z" * 100}]}]), 100)
check("binary placeholder counted as its text only",
      text_len([{"type": "text", "text": "[image content not shown]"}]), 25)

print("\nFlags")
check("long cold thread flags CONTEXT_REPAYMENT",
      "CONTEXT_REPAYMENT" in flags_for(long_, 0, "claude-opus-5"), True)
check("big project flags FAT_PROJECT",
      "FAT_PROJECT" in flags_for(st, 300_000, "claude-opus-5"), True)
check("short cheap opus chat flags PREMIUM_ON_TRIVIAL",
      "PREMIUM_ON_TRIVIAL" in flags_for(analyze_turns(thread(2, COLD, chars=400)),
                                        0, "claude-opus-5"), True)
check("healthy short sonnet thread has no flags",
      flags_for(analyze_turns(thread(4, WARM, chars=800, model="claude-sonnet-5")),
                0, "claude-sonnet-5"), [])

print("\nCounterfactuals")
cf = counterfactuals(long_, "claude-opus-5", 100_000)
check("splitting a 40-turn thread saves money",
      cf["if_split_every_8_turns"] < cf["actual_reconstructed"], True)
check("sonnet is cheaper than opus", cf["if_sonnet"] < cf["actual_reconstructed"], True)
check("trimming the project saves money",
      cf["if_project_trimmed_80pct"] < cf["actual_reconstructed"], True)
check("warm cache is cheaper than cold",
      cf["if_cache_always_warm"] < cf["actual_reconstructed"], True)

print("\nEdge cases")
check("empty transcript does not divide by zero",
      analyze_turns([])["repayment_multiplier"], 0)
check("user-only transcript has no assistant turns",
      analyze_turns([{"role": "user", "created_at": None,
                      "content": [{"type": "text", "text": "hi"}]}])["assistant_turns"], 0)
check("missing timestamps do not crash",
      analyze_turns([{"role": "assistant", "model": "claude-opus-5",
                      "created_at": None,
                      "content": [{"type": "text", "text": "hi"}]}])["assistant_turns"], 1)

print(f"\n{'=' * 62}\n  {PASS} passed, {FAIL} failed\n{'=' * 62}\n")
sys.exit(1 if FAIL else 0)
