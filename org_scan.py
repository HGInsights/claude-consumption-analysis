#!/usr/bin/env python3
"""
Org-wide scan: who spends, how concentrated it is, and how much of it is
long-lived context -- sessions left open for days and resumed.

    python3 org_scan.py --days 30                          # tier 0-1, no content
    python3 org_scan.py --days 30 --transcripts --deep 25  # tier 2: session shape
    python3 org_scan.py --days 30 --triggers               # tier 2: scheduled jobs

This is the step before cohort.py / analyze_user.py: it ranks every user in the
org so you can pick who to look at, and writes the top N to a file those tools
accept.

WHY LONG SESSIONS GET THEIR OWN SCAN
  Every request in a session re-sends the session's whole history. In a warm
  tool loop that re-payment is billed at the cache-read rate, which is why
  per-turn reconstruction overstates agentic sessions. But two things survive
  the cache: (1) Analytics prices requests above 200k context in a separate
  `context_window` bucket, so their real cost is measurable directly; and
  (2) a session resumed after the cache has expired re-sends everything
  uncached. A session left open for a week and picked up every few days pays
  that over and over. This is H1 (context re-payment) at agentic scale.

WHAT EACH TIER READS
  tier 0  Analytics cost by user x context_window x product x model. No content.
  tier 1  Compliance local-session list: created_at / updated_at only.
  tier 2  (--transcripts) message timestamps and sizes for the N longest-open
          sessions. Content is measured in memory and discarded; only shape
          numbers are written. Needs the same sign-off as any tier-2 read.
  tier 2  (--triggers) the opening message of EVERY session, to find
          unattended scheduled jobs. Only the scheduled-task / skill name is
          kept; prompt text is discarded.

Dollars come from Analytics only. Transcript context sizes are approximate
both ways -- tool results are read truncated, and compaction is invisible --
so they are capped at the 1M window and never priced here.
"""

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from investigate_user import (Http, load_env, _can_read_analytics, list_orgs,
                              list_users, fetch_cost, page_walk, analyze_turns,
                              session_span_hours, is_long_context, is_premium,
                              classify_trigger,
                              money, bar, LONG_LIVED_SPAN_HOURS,
                              LONG_CONTEXT_TOKENS)

SCAN_DIMENSIONS = ("model", "product", "context_window")
WEEK_H = 24 * 7


def analytics_client(env):
    key = env.get("ANTHROPIC_ANALYTICS_KEY") or ""
    if not _can_read_analytics(key):
        if not _can_read_analytics(env["ANTHROPIC_COMPLIANCE_KEY"]):
            raise SystemExit("No key can read the Analytics cost endpoints.")
        if key:
            print("note: ANTHROPIC_ANALYTICS_KEY lacks read:analytics; "
                  "using the compliance key.")
        key = env["ANTHROPIC_COMPLIANCE_KEY"]
    return Http(key, "Analytics")


def summarise_cost(rows):
    by_ctx, by_product, by_model = defaultdict(float), defaultdict(float), defaultdict(float)
    reqs_ctx = defaultdict(int)
    for r in rows:
        by_ctx[r["context_window"] or "unknown"] += r["amount"]
        reqs_ctx[r["context_window"] or "unknown"] += r["requests"]
        by_product[r["product"] or "unknown"] += r["amount"]
        by_model[r["model"] or "unknown"] += r["amount"]
    total = sum(by_ctx.values())
    long_ = sum(v for k, v in by_ctx.items() if is_long_context(k))
    prem = sum(v for k, v in by_model.items() if is_premium(k))
    return {
        "cost": total,
        "long_context_cost": long_,
        "long_context_share": long_ / total if total else 0,
        "premium_share": prem / total if total else 0,
        "by_context_window": dict(by_ctx),
        "requests_by_context_window": dict(reqs_ctx),
        "by_product": dict(by_product),
        "by_model": dict(by_model),
        "top_product": max(by_product, key=by_product.get) if by_product else "",
        "top_model": max(by_model, key=by_model.get) if by_model else "",
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=25,
                    help="write the top N spenders to out/top_spenders.txt")
    ap.add_argument("--transcripts", action="store_true",
                    help="tier 2: measure shape of the longest-open sessions")
    ap.add_argument("--deep", type=int, default=25,
                    help="with --transcripts, how many longest-open sessions to read")
    ap.add_argument("--tool-result-bytes", type=int, default=2000,
                    help="truncate tool results to this when reading; smaller is faster "
                         "but understates context size")
    ap.add_argument("--triggers", action="store_true",
                    help="tier 2: classify every session's trigger to find scheduled jobs")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    env = load_env()
    if not env.get("ANTHROPIC_COMPLIANCE_KEY"):
        raise SystemExit("ANTHROPIC_COMPLIANCE_KEY missing from .env")
    comp = Http(env["ANTHROPIC_COMPLIANCE_KEY"], "Compliance")
    analytics = analytics_client(env)

    end = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    start = end - timedelta(days=args.days)
    S = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    days = max(1, args.days)

    print(f"\n{'=' * 78}")
    print(f"  ORG SCAN — {start:%Y-%m-%d} to {end:%Y-%m-%d} ({args.days}d, ending 2d back)"
          f"{'  · transcripts ON (tier 2)' if args.transcripts else ''}")
    print(f"{'=' * 78}\n")

    # ---- tier 0: org total and per-user cost --------------------------------
    print("[1/4] Analytics: org total by context window...")
    org_rows, refreshed = fetch_cost(analytics, None, start, end, SCAN_DIMENSIONS)
    org = summarise_cost(org_rows)
    print(f"      {money(org['cost'])}  (refreshed {refreshed})")

    print("[2/4] Analytics: per-user cost (one call per member)...")
    users = {}
    members = list(list_users(comp, list_orgs(comp)))
    for i, (u, o) in enumerate(members, 1):
        rows, _ = fetch_cost(analytics, u["id"], start, end, SCAN_DIMENSIONS)
        if not rows:
            continue
        rec = summarise_cost(rows)
        if rec["cost"] <= 0:
            continue
        rec.update({"user_id": u["id"], "email": u.get("email"),
                    "name": u.get("full_name"), "org": o.get("name")})
        users[u["id"]] = rec
        if i % 25 == 0:
            print(f"      {i}/{len(members)} members, {len(users)} with spend")
    print(f"      {len(users)} of {len(members)} members had spend")

    # ---- tier 1: session ages from list metadata ----------------------------
    print("[3/4] Compliance: local session ages (metadata only)...")
    sessions = []
    for s in page_walk(comp, "/v1/compliance/apps/sessions/local",
                       {"created_at.gte": S, "limit": 500}, max_pages=1000):
        uid = (s.get("user") or {}).get("id")
        sessions.append({"id": s["id"], "user_id": uid,
                         "surface": s.get("product_surface"),
                         "created_at": s.get("created_at"),
                         "updated_at": s.get("updated_at"),
                         "span_h": round(session_span_hours(s), 1)})
    for rec in users.values():
        rec.update({"n_sessions": 0, "n_open_over_24h": 0,
                    "n_open_over_7d": 0, "max_span_h": 0.0})
    for s in sessions:
        rec = users.get(s["user_id"])
        if not rec:
            continue
        rec["n_sessions"] += 1
        rec["n_open_over_24h"] += s["span_h"] > LONG_LIVED_SPAN_HOURS
        rec["n_open_over_7d"] += s["span_h"] > WEEK_H
        rec["max_span_h"] = max(rec["max_span_h"], s["span_h"])
    print(f"      {len(sessions)} sessions, "
          f"{sum(1 for s in sessions if s['span_h'] > LONG_LIVED_SPAN_HOURS)} open >24h")

    # ---- tier 2: shape of the longest-open sessions -------------------------
    deep = []
    if args.transcripts:
        print(f"[4/4] Transcripts (tier 2): shape of the {args.deep} longest-open sessions...")
        for s in sorted(sessions, key=lambda s: -s["span_h"])[:args.deep]:
            msgs = list(page_walk(
                comp, f"/v1/compliance/apps/sessions/local/{s['id']}/messages",
                {"limit": 1000, "tool_result_max_bytes": args.tool_result_bytes},
                max_pages=100))
            st = analyze_turns(msgs)
            del msgs                          # classify then discard
            deep.append({**s, "messages": st["total_messages"],
                         "assistant_turns": st["assistant_turns"],
                         "tool_calls": st["tool_calls"],
                         "model": st["model"],
                         "active_days": st["active_days"],
                         "idle_resumes": st["idle_resumes"],
                         "stale_resumes": st["stale_resumes"],
                         "stale_resume_tokens": st["stale_resume_tokens"],
                         "peak_context_tokens": st["peak_context_tokens"],
                         "history_exceeds_window": st["history_exceeds_window"]})
            print(f"      {s['span_h']:>6,.0f}h  {st['total_messages']:>6,} msgs  "
                  f"{st['active_days']:>2} days  {st['stale_resumes']:>3} stale resumes  "
                  f"peak ~{st['peak_context_tokens']:>9,} tok  [{s['surface']}]")
    else:
        print("[4/4] Skipping transcripts. Add --transcripts for session shape (tier 2).")

    jobs = []
    if args.triggers:
        print(f"[+] Triggers (tier 2): opening message of {len(sessions):,} sessions...")
        classify_sessions(comp, sessions)
        jobs = summarise_jobs(sessions, users)
        price_jobs(analytics, jobs, sessions, users, start, end)

    ranked = sorted(users.values(), key=lambda r: -r["cost"])
    report(org, ranked, sessions, deep, days)
    if args.triggers:
        report_jobs(jobs, sessions, users, days)
    write(args, start, end, refreshed, org, ranked, sessions, deep, jobs)


# ------------------------------------------------------------ unattended ---
def classify_sessions(comp, sessions, workers=5):
    """Tag each session with its trigger. Reads one small page per session."""
    def one(s):
        try:
            msgs = list(page_walk(
                comp, f"/v1/compliance/apps/sessions/local/{s['id']}/messages",
                {"limit": 8, "tool_result_max_bytes": 100}, max_pages=1))
        except SystemExit:
            return s, ("error", None), None
        model = next((m.get("model") for m in msgs
                      if m.get("role") == "assistant" and m.get("model")), None)
        return s, classify_trigger(msgs), model      # msgs discarded here
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (s, (kind, name), model) in enumerate(pool.map(one, sessions), 1):
            s["trigger"], s["job"], s["model"] = kind, name, model
            if i % 1000 == 0:
                print(f"      {i:,}/{len(sessions):,}")


def summarise_jobs(sessions, users):
    by = defaultdict(list)
    for s in sessions:
        if s.get("trigger") == "scheduled":
            by[(s["user_id"], s["job"])].append(s)
    jobs = []
    for (uid, name), runs in by.items():
        dates = sorted({r["created_at"][:10] for r in runs})
        weekend = sum(1 for r in runs
                      if datetime.fromisoformat(r["created_at"][:10]).weekday() >= 5)
        jobs.append({"user_id": uid, "owner": (users.get(uid) or {}).get("name"),
                     "email": (users.get(uid) or {}).get("email"),
                     "job": name, "runs": len(runs), "days": len(dates),
                     "weekend_runs": weekend, "first": dates[0], "last": dates[-1],
                     "surface": Counter(r["surface"] for r in runs).most_common(1)[0][0],
                     "models": dict(Counter(r.get("model") or "?" for r in runs))})
    return sorted(jobs, key=lambda j: -j["runs"])


def price_jobs(analytics, jobs, sessions, users, start, end):
    """
    Estimate scheduled-job cost from real Analytics dollars. On a "clean day"
    -- every session the owner started that day was a scheduled run -- the
    owner's agentic spend that day is the jobs' spend. Median cost per run
    over clean days, times runs, gives the estimate. Owners with no clean day
    stay unpriced rather than guessed.
    """
    owners = {j["user_id"] for j in jobs}
    day_kinds = defaultdict(lambda: defaultdict(Counter))   # uid -> day -> trigger counts
    for s in sessions:
        if s["user_id"] in owners:
            day_kinds[s["user_id"]][s["created_at"][:10]][s.get("trigger")] += 1
    per_run = {}
    for uid in owners:
        rows, _ = fetch_cost(analytics, uid, start, end, ("product",))
        daily = defaultdict(float)
        for r in rows:
            if r["product"] in ("cowork", "claude_code", "office_agent"):
                daily[r["date"]] += r["amount"]
        samples = []
        for day, kinds in day_kinds[uid].items():
            n = kinds.get("scheduled", 0)
            if n and n == sum(kinds.values()) and day in daily:
                samples.append(daily[day] / n)
        if samples:
            samples.sort()
            per_run[uid] = (samples[len(samples) // 2], len(samples))
    for j in jobs:
        est = per_run.get(j["user_id"])
        j["est_cost_per_run"] = round(est[0], 2) if est else None
        j["clean_days"] = est[1] if est else 0
        j["est_monthly_cost"] = round(est[0] * j["runs"], 2) if est else None


def report_jobs(jobs, sessions, users, days):
    W = 78
    kinds = Counter(s.get("trigger") for s in sessions)
    sched = kinds.get("scheduled", 0)
    print(f"\n{'=' * W}\n  UNATTENDED SCHEDULED JOBS\n{'=' * W}\n")
    print(f"  Session triggers: {dict(kinds)}")
    print(f"  {sched:,} of {len(sessions):,} sessions ({sched / (len(sessions) or 1) * 100:.0f}%)"
          f" were scheduled runs: {len(jobs)} jobs, "
          f"{len({j['user_id'] for j in jobs})} owners.")
    priced = [j for j in jobs if j["est_monthly_cost"] is not None]
    if priced:
        tot = sum(j["est_monthly_cost"] for j in priced)
        print(f"  Priced from clean days: {len(priced)}/{len(jobs)} jobs, "
              f"~{money(tot)} this window (estimate, not a measurement).")
    wk = sum(j["weekend_runs"] for j in jobs)
    print(f"  Weekend runs: {wk:,}")
    print(f"\n  {'owner':<22}{'job':<36}{'runs':>5}{'days':>5}{'wknd':>5}{'~$/run':>8}{'~$/mo':>8}")
    print(f"  {'-' * 76}")
    for j in jobs[:30]:
        print(f"  {(j['owner'] or '?')[:21]:<22}{j['job'][:35]:<36}{j['runs']:>5}{j['days']:>5}"
              f"{j['weekend_runs']:>5}"
              f"{(money(j['est_cost_per_run']) if j['est_cost_per_run'] is not None else '-'):>8}"
              f"{(money(j['est_monthly_cost']) if j['est_monthly_cost'] is not None else '-'):>8}")


def report(org, ranked, sessions, deep, days):
    W = 78
    attributed = sum(r["cost"] for r in ranked)
    print(f"\n{'=' * W}\n  SPEND\n{'=' * W}\n")
    print(f"  Org total (Analytics)       {money(org['cost']):>12}   "
          f"{money(org['cost'] / days * 365)}/yr")
    print(f"  Attributed to members       {money(attributed):>12}   "
          f"{attributed / (org['cost'] or 1) * 100:.0f}%  "
          f"(gap = API keys, departed users)")
    print(f"  Users with spend            {len(ranked):>12}")
    if ranked:
        costs = [r["cost"] for r in ranked]
        print(f"  Median / mean per user      {money(costs[len(costs) // 2]):>12} / "
              f"{money(sum(costs) / len(costs))}")
    print("\n  Concentration")
    for n in (5, 10, 20, 30, 50, 100):
        if n > len(ranked):
            break
        c = sum(r["cost"] for r in ranked[:n])
        print(f"    top {n:<4} {money(c):>11}  {bar(c / (attributed or 1))} "
              f"{c / (attributed or 1) * 100:4.1f}%")

    print(f"\n{'=' * W}\n  LONG-LIVED CONTEXT  (H1 at agentic scale)\n{'=' * W}\n")
    reqs = org["requests_by_context_window"]
    for k, v in sorted(org["by_context_window"].items(), key=lambda x: -x[1]):
        n = reqs.get(k, 0)
        print(f"  {k:<10} {money(v):>11}  {bar(v / (org['cost'] or 1))} "
              f"{v / (org['cost'] or 1) * 100:4.1f}%   {n:>8,} req   "
              f"{money(v / n) if n else '-':>7}/req")
    long_users = [r for r in ranked if r["long_context_share"] > 0.5]
    lc = sum(r["long_context_cost"] for r in ranked)
    print(f"\n  {len(long_users)} users spend >50% of their bill on >200k-context requests.")
    print(f"  Long-context dollars are {lc / (attributed or 1) * 100:.0f}% of attributed spend.")

    spans = [s["span_h"] for s in sessions]
    if spans:
        print(f"\n  Sessions open >24h   {sum(1 for h in spans if h > LONG_LIVED_SPAN_HOURS):>6,}"
              f"   of {len(spans):,}")
        print(f"  Sessions open >7d    {sum(1 for h in spans if h > WEEK_H):>6,}")
        print(f"  Longest              {max(spans):>6,.0f}h  ({max(spans) / 24:.0f} days)")

    print("\n  TOP 15 BY LONG-CONTEXT DOLLARS")
    print(f"  {'user':<26}{'cost':>10}{'>200k $':>10}{'share':>7}"
          f"{'>24h':>6}{'>7d':>5}{'longest':>9}  top product")
    print(f"  {'-' * 76}")
    for r in sorted(ranked, key=lambda r: -r["long_context_cost"])[:15]:
        print(f"  {(r['name'] or r['email'] or '?')[:25]:<26}{money(r['cost']):>10}"
              f"{money(r['long_context_cost']):>10}{r['long_context_share'] * 100:>6.0f}%"
              f"{r['n_open_over_24h']:>6}{r['n_open_over_7d']:>5}"
              f"{r['max_span_h'] / 24:>8.1f}d  {r['top_product']}")

    if deep:
        print("\n  LONGEST-OPEN SESSIONS (tier 2 shape; context approximate, capped at 1M)")
        stale = [d for d in deep if d["stale_resumes"]]
        big = [d for d in deep if d["peak_context_tokens"] >= LONG_CONTEXT_TOKENS]
        print(f"  {len(big)}/{len(deep)} reached >200k context; "
              f"{len(stale)}/{len(deep)} were resumed after >1h idle carrying >100k.")
        over = sum(1 for d in deep if d.get("history_exceeds_window"))
        if over:
            print(f"  {over}/{len(deep)} accumulated more history than fits in 1M "
                  f"-- compacted at least once.")
        resent = sum(d["stale_resume_tokens"] for d in deep)
        print(f"  Context re-sent uncached on those resumes: ~{resent / 1e6:,.1f}M tokens.")
        active = sorted(d["active_days"] / max(1, d["span_h"] / 24) for d in deep)
        print(f"  Median active days / open days: {active[len(active) // 2] * 100:.0f}% "
              f"-- sessions are mostly idle, then resumed.")
    print()


def write(args, start, end, refreshed, org, ranked, sessions, deep, jobs=()):
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "org_scan.json"), "w") as f:
        json.dump({"window": {"start": start.isoformat(), "end": end.isoformat(),
                              "days": args.days, "data_refreshed_at": refreshed},
                   "org": org, "users": ranked,
                   "longest_sessions": sorted(sessions, key=lambda s: -s["span_h"])[:200],
                   "deep_sessions": deep, "scheduled_jobs": list(jobs)},
                  f, indent=2, default=str)
    cols = ["email", "name", "cost", "long_context_cost", "long_context_share",
            "premium_share", "n_sessions", "n_open_over_24h", "n_open_over_7d",
            "max_span_h", "top_product", "top_model"]
    with open(os.path.join(args.out, "org_scan_users.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in ranked:
            w.writerow([round(r[c], 3) if isinstance(r[c], float) else r[c] for c in cols])
    if deep:
        email = {r["user_id"]: r["email"] for r in ranked}
        cols = ["id", "surface", "span_h", "messages", "assistant_turns", "tool_calls",
                "active_days", "idle_resumes", "stale_resumes", "stale_resume_tokens",
                "peak_context_tokens", "history_exceeds_window", "model"]
        with open(os.path.join(args.out, "org_scan_sessions.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["email"] + cols)
            for d in deep:
                w.writerow([email.get(d["user_id"], "")] + [d.get(c) for c in cols])
    with open(os.path.join(args.out, "top_spenders.txt"), "w") as f:
        for r in ranked[:args.top]:
            if r.get("email"):
                f.write(r["email"] + "\n")
    print(f"Wrote {args.out}/org_scan.json, org_scan_users.csv"
          f"{', org_scan_sessions.csv' if deep else ''}, top_spenders.txt")


if __name__ == "__main__":
    main()
