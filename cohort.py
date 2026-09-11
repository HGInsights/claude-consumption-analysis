#!/usr/bin/env python3
"""
Run the spend investigation across several users and look for shared patterns.

    python3 cohort.py --days 30 --emails a@example.com b@example.com
    python3 cohort.py --days 30 --file cohort.txt --transcripts

Metadata by default (no content read). --transcripts adds session shape
(turn counts, tool calls, inter-turn gaps), which is what distinguishes an
agent loop from a human conversation. Tier 2 -- needs sign-off.

Reuses investigate_user.py for all API work so the two stay consistent.
"""

import argparse
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from investigate_user import (Http, load_env, _can_read_analytics, resolve_user,
                              fetch_cost, fetch_usage, page_walk, cursor_walk,
                              analyze_turns, is_premium, money, bar, wrap, toks)

AGENTIC_PRODUCTS = ("cowork", "claude_code", "office_agent")


def collect(email, comp, analytics, start, end, transcripts, max_sessions):
    """Everything we know about one user. Returns None if they can't be found."""
    try:
        user, org = resolve_user(comp, email)
    except SystemExit as e:
        print(f"    SKIP {email}: {str(e).strip().splitlines()[0]}")
        return None

    uid = user["id"]
    cost_rows, refreshed = fetch_cost(analytics, uid, start, end)
    usage, usage_by_product = fetch_usage(analytics, uid, start, end)
    actual = sum(r["amount"] for r in cost_rows)

    by_model, by_product = defaultdict(float), defaultdict(float)
    for r in cost_rows:
        by_model[r["model"] or "unknown"] += r["amount"]
        by_product[r["product"] or "unknown"] += r["amount"]

    chats = list(cursor_walk(comp, "/v1/compliance/apps/chats", {
        "user_ids[]": [uid],
        "created_at.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 100}))
    sessions = [s for s in page_walk(comp, "/v1/compliance/apps/sessions/local", {
        "created_at.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 500}) if (s.get("user") or {}).get("id") == uid]
    remote = list(page_walk(comp, "/v1/compliance/apps/sessions/remote", {
        "user_ids[]": [uid],
        "created_at.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 500}))

    surfaces = defaultdict(int)
    for s in sessions:
        surfaces[s.get("product_surface") or "unknown"] += 1

    rec = {
        "email": email,
        "name": user.get("full_name"),
        "user_id": uid,
        "role": user.get("organization_role"),
        "cost": actual,
        "by_model": dict(by_model),
        "by_product": dict(by_product),
        "usage": usage,
        "n_chats": len(chats),
        "n_local": len(sessions),
        "n_remote": len(remote),
        "surfaces": dict(surfaces),
        "sessions": [],
    }

    if transcripts and sessions:
        for s in sorted(sessions, key=lambda x: x.get("updated_at") or "",
                        reverse=True)[:max_sessions]:
            msgs = list(page_walk(
                comp, f"/v1/compliance/apps/sessions/local/{s['id']}/messages",
                {"limit": 500, "tool_result_max_bytes": 2000}))
            if not msgs:
                continue
            st = analyze_turns(msgs)
            st["id"] = s["id"]
            st["surface"] = s.get("product_surface")
            rec["sessions"].append(st)
    return rec


def derive(r):
    """Ratios that separate an agent loop from a human conversation."""
    u = r["usage"]
    total_in = (u.get("uncached_input", 0) + u.get("cache_read", 0)
                + u.get("cache_write", 0))
    reqs = u.get("requests", 0) or 0
    out = u.get("output", 0) or 0
    cost = r["cost"]
    prem = sum(v for k, v in r["by_model"].items() if is_premium(k))
    fable = sum(v for k, v in r["by_model"].items() if "fable" in (k or "").lower())
    agentic = sum(v for k, v in r["by_product"].items()
                  if any(p in (k or "") for p in AGENTIC_PRODUCTS))
    gaps = [s["median_gap_s"] for s in r["sessions"]] or []
    turns = [s["assistant_turns"] for s in r["sessions"]] or []
    tools = sum(s["tool_calls"] for s in r["sessions"])
    return {
        "cost": cost,
        "tokens_in_per_req": total_in / reqs if reqs else 0,
        "in_out_ratio": total_in / out if out else 0,
        "cache_hit": u.get("cache_read", 0) / total_in if total_in else 0,
        "premium_share": prem / cost if cost else 0,
        "fable_share": fable / cost if cost else 0,
        "agentic_share": agentic / cost if cost else 0,
        "requests": reqs,
        "median_gap": statistics.median(gaps) if gaps else None,
        "max_turns": max(turns) if turns else 0,
        "total_turns": sum(turns),
        "tool_calls": tools,
        "sessions_sampled": len(r["sessions"]),
    }


def classify(r, d):
    """
    Which pattern is this user? Returns (label, confidence, evidence).
    The signature: agentic surface + premium model + no think time.
    """
    ev = []
    if d["agentic_share"] > 0.7:
        ev.append(f"{d['agentic_share']*100:.0f}% agentic surface")
    if d["fable_share"] > 0.5:
        ev.append(f"{d['fable_share']*100:.0f}% Fable")
    elif d["premium_share"] > 0.6:
        ev.append(f"{d['premium_share']*100:.0f}% premium")
    if d["median_gap"] is not None and d["median_gap"] <= 2:
        ev.append(f"median gap {d['median_gap']:.0f}s (no human think time)")
    if d["max_turns"] > 500:
        ev.append(f"{d['max_turns']:,}-turn session")
    if d["in_out_ratio"] > 50:
        ev.append(f"{d['in_out_ratio']:.0f}:1 input:output")

    agentic = d["agentic_share"] > 0.7
    premium = d["premium_share"] > 0.6
    loop = (d["median_gap"] is not None and d["median_gap"] <= 2
            and d["max_turns"] > 100)

    if agentic and premium and loop:
        return "AGENT_LOOP_ON_PREMIUM", "STRONG", ev
    if agentic and premium:
        return "AGENTIC_ON_PREMIUM", "MODERATE", ev
    if agentic:
        return "AGENTIC_ON_APPROPRIATE_MODEL", "MODERATE", ev
    if premium and d["tokens_in_per_req"] > 80_000:
        return "HEAVY_CHAT_ON_PREMIUM", "MODERATE", ev
    if premium:
        return "CHAT_ON_PREMIUM", "WEAK", ev
    return "UNREMARKABLE", "WEAK", ev or ["no pattern flag fired"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--emails", nargs="*", default=[])
    ap.add_argument("--file")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--transcripts", action="store_true")
    ap.add_argument("--max-sessions", type=int, default=25)
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    emails = list(args.emails)
    if args.file:
        with open(args.file) as f:
            emails += [l.strip() for l in f
                       if l.strip() and not l.startswith("#")]
    if not emails:
        raise SystemExit("Give --emails or --file")

    env = load_env()
    comp = Http(env["ANTHROPIC_COMPLIANCE_KEY"], "Compliance")
    akey = env.get("ANTHROPIC_ANALYTICS_KEY") or ""
    if not _can_read_analytics(akey):
        if not _can_read_analytics(env["ANTHROPIC_COMPLIANCE_KEY"]):
            raise SystemExit("No key can read the Analytics cost endpoints.")
        akey = env["ANTHROPIC_COMPLIANCE_KEY"]
        print("note: ANTHROPIC_ANALYTICS_KEY lacks read:analytics; "
              "using the compliance key.")
    analytics = Http(akey, "Analytics")

    end = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    start = end - timedelta(days=args.days)

    print(f"\n{'=' * 78}")
    print(f"  COHORT ANALYSIS — {len(emails)} users")
    print(f"  {start:%Y-%m-%d} to {end:%Y-%m-%d} ({args.days}d)"
          f"{'  · transcripts ON (tier 2)' if args.transcripts else '  · metadata only'}")
    print(f"{'=' * 78}\n")

    recs = []
    for i, em in enumerate(emails, 1):
        print(f"[{i}/{len(emails)}] {em}")
        r = collect(em, comp, analytics, start, end,
                    args.transcripts, args.max_sessions)
        if not r:
            continue
        r["derived"] = derive(r)
        r["pattern"], r["confidence"], r["evidence"] = classify(r, r["derived"])
        recs.append(r)
        d = r["derived"]
        print(f"    {money(d['cost']):>10}  {r['pattern']}"
              f"  ({r['n_local']} local / {r['n_chats']} chats)")

    if not recs:
        raise SystemExit("No users resolved.")

    recs.sort(key=lambda r: -r["cost"])
    report(recs, args, start, end)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "cohort.json"), "w") as f:
        json.dump({"window": {"start": start.isoformat(), "end": end.isoformat(),
                              "days": args.days},
                   "users": recs}, f, indent=2, default=str)
    path = os.path.join(args.out, "cohort.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["email", "name", "cost_usd", "pattern", "confidence",
                    "agentic_share", "premium_share", "fable_share",
                    "tokens_in_per_req", "in_out_ratio", "cache_hit",
                    "requests", "median_gap_s", "max_turns", "tool_calls",
                    "n_chats", "n_local_sessions", "top_product", "top_model"])
        for r in recs:
            d = r["derived"]
            tp = max(r["by_product"], key=r["by_product"].get) if r["by_product"] else ""
            tm = max(r["by_model"], key=r["by_model"].get) if r["by_model"] else ""
            w.writerow([r["email"], r["name"], round(r["cost"], 2), r["pattern"],
                        r["confidence"], round(d["agentic_share"], 3),
                        round(d["premium_share"], 3), round(d["fable_share"], 3),
                        round(d["tokens_in_per_req"]), round(d["in_out_ratio"], 1),
                        round(d["cache_hit"], 3), d["requests"],
                        d["median_gap"], d["max_turns"], d["tool_calls"],
                        r["n_chats"], r["n_local"], tp, tm])
    print(f"\nWrote {path} and out/cohort.json")


def report(recs, args, start, end):
    W = 78
    total = sum(r["cost"] for r in recs)

    print(f"\n{'=' * W}")
    print("  SPEND")
    print(f"{'=' * W}\n")
    print(f"  {'user':<26}{'cost':>10}  {'/day':>8}  {'pattern':<28}")
    print(f"  {'-' * 74}")
    for r in recs:
        print(f"  {(r['name'] or r['email'])[:25]:<26}{money(r['cost']):>10}"
              f"  {money(r['cost']/args.days):>8}  {r['pattern']:<28}")
    print(f"  {'-' * 74}")
    print(f"  {'TOTAL':<26}{money(total):>10}  {money(total/args.days):>8}")
    print(f"\n  Annualised for this cohort: {money(total / args.days * 365)}")

    print(f"\n{'=' * W}")
    print("  THE SIGNATURE TEST  (agentic surface + premium model + no think time)")
    print(f"{'=' * W}\n")
    hdr = (f"  {'user':<20}{'agentic':>9}{'premium':>9}{'fable':>8}"
           f"{'tok/req':>10}{'in:out':>9}{'gap':>7}{'maxturns':>10}")
    print(hdr)
    print(f"  {'-' * 74}")
    for r in recs:
        d = r["derived"]
        gap = "-" if d["median_gap"] is None else f"{d['median_gap']:.0f}s"
        print(f"  {(r['name'] or r['email']).split()[0][:19]:<20}"
              f"{d['agentic_share']*100:>8.0f}%{d['premium_share']*100:>8.0f}%"
              f"{d['fable_share']*100:>7.0f}%{d['tokens_in_per_req']:>10,.0f}"
              f"{d['in_out_ratio']:>8.0f}:1{gap:>7}{d['max_turns']:>10,}")

    # ---- shared patterns ---------------------------------------------------
    print(f"\n{'=' * W}")
    print("  PATTERNS ACROSS THE COHORT")
    print(f"{'=' * W}\n")

    groups = defaultdict(list)
    for r in recs:
        groups[r["pattern"]].append(r)
    for pat, members in sorted(groups.items(), key=lambda x: -sum(m["cost"] for m in x[1])):
        c = sum(m["cost"] for m in members)
        print(f"  {pat}   {len(members)} user(s)   {money(c)}"
              f"   {c/total*100:.0f}% of cohort spend")
        for m in members:
            print(f"      {(m['name'] or m['email'])[:28]:<30} {money(m['cost']):>9}"
                  f"   {'; '.join(m['evidence'][:3])}")
        print()

    # model concentration
    model_tot = defaultdict(float)
    prod_tot = defaultdict(float)
    for r in recs:
        for k, v in r["by_model"].items():
            model_tot[k] += v
        for k, v in r["by_product"].items():
            prod_tot[k] += v

    print("  COHORT SPEND BY MODEL")
    for k, v in sorted(model_tot.items(), key=lambda x: -x[1]):
        tag = " PREMIUM" if is_premium(k) else ""
        print(f"    {k:<28}{money(v):>10}  {bar(v/(total or 1))} "
              f"{v/(total or 1)*100:4.1f}%{tag}")
    print("\n  COHORT SPEND BY PRODUCT")
    for k, v in sorted(prod_tot.items(), key=lambda x: -x[1]):
        print(f"    {k:<28}{money(v):>10}  {bar(v/(total or 1))} "
              f"{v/(total or 1)*100:4.1f}%")

    # ---- the one lever -----------------------------------------------------
    print(f"\n{'=' * W}")
    print("  LEVER SIZING")
    print(f"{'=' * W}\n")

    fable_spend = sum(v for k, v in model_tot.items() if "fable" in (k or "").lower())
    opus_spend = sum(v for k, v in model_tot.items() if "opus" in (k or "").lower())
    # Fable/Opus -> Sonnet is a 5x / 2.5x list-price cut on the same token volume.
    save_fable = fable_spend * (1 - 1/5)
    save_opus = opus_spend * (1 - 1/2.5)
    print(f"  Fable spend in cohort          {money(fable_spend):>10}")
    print(f"  Opus spend in cohort           {money(opus_spend):>10}")
    print(f"\n  If Fable work moved to Sonnet  {money(save_fable):>10} saved "
          f"({save_fable/args.days*365:,.0f}/yr)")
    print(f"  If Opus work moved to Sonnet   {money(save_opus):>10} saved "
          f"({save_opus/args.days*365:,.0f}/yr)")
    print(f"\n  Caveat: assumes the work tolerates Sonnet. Validate on the")
    print(f"  agentic users first -- long tool loops are where a model")
    print(f"  downgrade is most likely to cost more in retries than it saves.")

    agentic_users = [r for r in recs if r["derived"]["agentic_share"] > 0.7]
    if agentic_users:
        ac = sum(r["cost"] for r in agentic_users)
        print(f"\n  {len(agentic_users)}/{len(recs)} users are >70% agentic, "
              f"carrying {money(ac)} ({ac/total*100:.0f}% of cohort spend).")

    if not args.transcripts:
        print(f"\n  Note: run with --transcripts to fill in gap/turn columns,")
        print(f"  which is what separates an agent loop from a human conversation.")
    print()


if __name__ == "__main__":
    main()
