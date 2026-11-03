#!/usr/bin/env python3
"""
One-shot per-user spend audit. Emits a single JSON blob with everything
needed to write a profile: cost, model/product mix, session triggers,
recurring jobs, tool mix, duplicate-call analysis, and project weights.

    python3 analyze_user.py someone@example.com --days 30 --out out/audits

Designed to be run unattended (e.g. by a subagent) and produce consistent,
comparable output across users. Stdlib only.
"""
import argparse, collections, json, os, re, sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from investigate_user import (Http, load_env, _can_read_analytics, resolve_user,
                              fetch_cost, fetch_usage, page_walk, cursor_walk,
                              analyze_turns, is_premium, toks, session_span_hours,
                              is_long_context, LONG_LIVED_SPAN_HOURS,
                              LONG_CONTEXT_TOKENS, classify_trigger)

AGENTIC = ("cowork", "claude_code", "office_agent")

# Tools whose *repetition with identical input* indicates a missing cache
# rather than real work. Retrieval is read-only and deterministic, so calling
# it twice with the same argument is waste; a write or a compute tool is not.
#
# This list is org-specific: your MCP servers are not ours. The defaults below
# are the surface-agnostic built-ins plus common SaaS connectors. To add your
# own, drop a `retrieval_tools.txt` next to this script, one bare tool name per
# line (`#` comments allowed) — it is gitignored, so your internal tool names
# never reach a commit. Names are matched after `mcp__server__` is stripped.
DEFAULT_RETRIEVAL = (
    # Claude built-ins
    "WebSearch", "WebFetch", "Read", "Grep", "Glob",
    # common connector verbs
    "read_file_content", "get_thread", "search_threads", "slack_read_channel",
    "jira_search_tickets", "jira_get_ticket", "get_research_result",
)
# Fallback for connectors we have never seen: a bare-name prefix heuristic.
# Read-only verbs only — never `create_`, `update_`, `send_`, `delete_`.
RETRIEVAL_PREFIXES = ("search_", "get_", "list_", "lookup_", "fetch_", "query_", "read_")


def _load_retrieval():
    """Built-in retrieval names plus any from retrieval_tools.txt."""
    names = set(DEFAULT_RETRIEVAL)
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "retrieval_tools.txt")
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    names.add(line)
    except OSError:
        pass
    return names


RETRIEVAL = _load_retrieval()


def is_retrieval(name):
    """True if a repeated identical call to `name` is waste, not work."""
    if name in RETRIEVAL:
        return True
    return name.startswith(RETRIEVAL_PREFIXES)


# Coarse error buckets, matched against the first few hundred bytes of an
# error result in memory. The text itself is never stored or written out.
ERROR_KINDS = (
    ("timeout", r"timed? ?out|timeout|deadline exceeded"),
    ("auth", r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|permission denied|"
             r"not authenticated|invalid (api )?key|token (has )?expired|reauth"),
    ("rate_limit", r"\b429\b|rate.?limit|too many requests|quota"),
    ("not_found", r"\b404\b|not found|does not exist|no such file|unknown (tool|id)"),
    ("too_large", r"too large|exceeds|maximum (allowed )?(size|length|tokens)|truncat"),
    ("bad_input", r"\b400\b|invalid|validation|required (field|param)|could not be parsed|"
                  r"unexpected (argument|keyword)|schema"),
    ("user_rejected", r"user (doesn.t want|rejected|denied|declined)|permission to use .* denied"),
    ("server_error", r"\b50[0234]\b|internal (server )?error|service unavailable|bad gateway"),
    ("exit_code", r"exit code|exited with|command failed|non-zero"),
)


def error_kind(text):
    t = (text or "").lower()
    for kind, pat in ERROR_KINDS:
        if re.search(pat, t):
            return kind
    return "other"


def connector_of(name):
    """mcp__<server>__tool -> <server>; built-ins -> 'builtin'."""
    n = name or ""
    return n.split("__")[1] if n.startswith("mcp__") and n.count("__") >= 2 else "builtin"


def shorten(name):
    return (name or "?").split("__")[-1] if (name or "").startswith("mcp__") else (name or "?")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("email")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max-sessions", type=int, default=14, help="sessions to deep-read")
    ap.add_argument("--long-sessions", type=int, default=4,
                    help="longest-open sessions to deep-read on top of the most recent")
    ap.add_argument("--scan-sessions", type=int, default=60, help="sessions to classify by trigger")
    ap.add_argument("--out", default="out/audits")
    a = ap.parse_args()

    env = load_env()
    comp = Http(env["ANTHROPIC_COMPLIANCE_KEY"], "Compliance")
    ak = env.get("ANTHROPIC_ANALYTICS_KEY") or ""
    if not _can_read_analytics(ak):
        ak = env["ANTHROPIC_COMPLIANCE_KEY"]
    an = Http(ak, "Analytics")

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) \
          - timedelta(days=2)
    start = end - timedelta(days=a.days)
    S = start.strftime("%Y-%m-%dT%H:%M:%SZ")

    user, org = resolve_user(comp, a.email)
    uid = user["id"]
    sys.stderr.write(f"[1/5] {user.get('full_name')} {uid}\n")

    cost_rows, refreshed = fetch_cost(an, uid, start, end)
    usage, usage_by_product = fetch_usage(an, uid, start, end)
    cost = sum(r["amount"] for r in cost_rows)
    by_model, by_product = collections.defaultdict(float), collections.defaultdict(float)
    by_ttype = collections.defaultdict(float)
    by_ctx = collections.defaultdict(float)
    for r in cost_rows:
        by_model[r["model"] or "?"] += r["amount"]
        by_product[r["product"] or "?"] += r["amount"]
        by_ttype[r["token_type"] or "?"] += r["amount"]
        by_ctx[r["context_window"] or "?"] += r["amount"]
    sys.stderr.write(f"[2/5] cost ${cost:,.2f}\n")

    chats = list(cursor_walk(comp, "/v1/compliance/apps/chats",
                 {"user_ids[]": [uid], "created_at.gte": S, "limit": 100}))
    sessions = [s for s in page_walk(comp, "/v1/compliance/apps/sessions/local",
                {"created_at.gte": S, "limit": 500})
                if (s.get("user") or {}).get("id") == uid]
    sys.stderr.write(f"[3/5] {len(chats)} chats, {len(sessions)} sessions\n")
    for s in sessions:
        s["_span_h"] = session_span_hours(s)
    spans = sorted((s["_span_h"] for s in sessions), reverse=True)

    # --- trigger classification (cheap: first page only) --------------------
    kinds = collections.Counter()
    recurring = collections.defaultdict(set)
    prompts, byhour = [], collections.Counter()
    for s in sessions[:a.scan_sessions]:
        byhour[int(s["created_at"][11:13])] += 1
        try:
            msgs = list(page_walk(comp,
                        f"/v1/compliance/apps/sessions/local/{s['id']}/messages",
                        {"limit": 8, "tool_result_max_bytes": 100}, max_pages=1))
        except SystemExit:
            continue
        txt = ""
        for m in msgs:
            if m.get("role") == "user" and not (m.get("provenance") or {}).get("type") and not txt:
                txt = "".join(b.get("text", "") for b in (m.get("content") or [])
                              if b.get("type") == "text")
        kind, name = classify_trigger(msgs)
        if kind == "scheduled":
            kinds["scheduled"] += 1; recurring[name].add(s["created_at"][:10])
        elif kind == "skill":
            kinds["skill"] += 1; recurring["SKILL " + name].add(s["created_at"][:10])
        elif kind == "ad-hoc":
            kinds["ad-hoc"] += 1
            c = re.sub(r'<system-reminder>.*?</system-reminder>', '', txt, flags=re.S)
            c = re.sub(r'<uploaded_files>.*?</uploaded_files>', '', c, flags=re.S).strip()
            if c:
                prompts.append(c[:240].replace("\n", " "))
        else:
            kinds["no-capture"] += 1
    sys.stderr.write(f"[4/5] triggers {dict(kinds)}\n")

    # --- deep read for determinism signals ----------------------------------
    tools = collections.Counter(); pairs = collections.Counter()
    inputs = collections.Counter(); retr_calls = []
    errors = 0; deep = []
    calls_by_tool = collections.Counter()            # full name incl. MCP server
    errors_by_tool = collections.defaultdict(collections.Counter)  # name -> kind -> n
    dup_within = collections.Counter()   # repeat of an identical call in the SAME session
    dup_across = collections.Counter()   # identical call first made in an EARLIER session
    seen_any = set()
    # The list is newest-first, so the most recent sessions alone would miss
    # exactly the long-lived ones. Add the longest-open few on top.
    recent = sessions[:a.max_sessions]
    seen = {s["id"] for s in recent}
    longest = [s for s in sorted(sessions, key=lambda s: -s["_span_h"])
               if s["id"] not in seen][:a.long_sessions]
    for s in recent + longest:
        try:
            msgs = list(page_walk(comp,
                        f"/v1/compliance/apps/sessions/local/{s['id']}/messages",
                        {"limit": 1000, "tool_result_max_bytes": 400}, max_pages=30))
        except SystemExit:
            continue
        seq = []
        seen_here = set()
        for m in msgs:
            for b in m.get("content") or []:
                t = b.get("type")
                if t == "tool_use":
                    full = b.get("name") or "?"
                    nm = shorten(full); seq.append(nm); tools[nm] += 1
                    calls_by_tool[full] += 1
                    inp = b.get("input")
                    if isinstance(inp, str) and len(inp) < 400:
                        inputs[(nm, inp[:200])] += 1
                        if is_retrieval(nm):
                            retr_calls.append((nm, inp[:200]))
                            key = (full, inp[:200])
                            # Re-reading in a new session is not waste: that
                            # context is gone. Only same-session repeats are.
                            if key in seen_here:
                                dup_within[full] += 1
                            elif key in seen_any:
                                dup_across[full] += 1
                            seen_here.add(key); seen_any.add(key)
                elif t == "tool_result" and b.get("is_error"):
                    errors += 1
                    txt = " ".join((e.get("text") or "")[:300]
                                   for e in (b.get("content") or []) if isinstance(e, dict))
                    errors_by_tool[b.get("name") or "?"][error_kind(txt)] += 1
        for x, y in zip(seq, seq[1:]):
            pairs[(x, y)] += 1
        st = analyze_turns(msgs)
        deep.append({"messages": len(msgs), "tool_calls": len(seq),
                     "assistant_turns": st["assistant_turns"],
                     "median_gap_s": st["median_gap_s"], "model": st["model"],
                     "surface": s.get("product_surface"),
                     "span_hours": round(s["_span_h"], 1),
                     "active_days": st["active_days"],
                     "peak_context_tokens": st["peak_context_tokens"],
                     "idle_resumes": st["idle_resumes"],
                     "stale_resumes": st["stale_resumes"],
                     "stale_resume_tokens": st["stale_resume_tokens"],
                     "history_exceeds_window": st["history_exceeds_window"]})

    dup_rate = 0.0
    if retr_calls:
        dup_rate = 1 - len(set(retr_calls)) / len(retr_calls)
    total_tools = sum(tools.values())

    # --- projects -----------------------------------------------------------
    pids = collections.Counter(c["project_id"] for c in chats if c.get("project_id"))
    projects = {}
    for pid, n in pids.most_common(6):
        try:
            att = list(page_walk(comp, f"/v1/compliance/apps/projects/{pid}/attachments",
                                 {"limit": 100}))
        except SystemExit:
            continue
        sz = sum(x.get("size_bytes") or 0 for x in att)
        docs = sum(1 for x in att if x.get("type") == "project_doc")
        projects[pid] = {"chats": n, "files": len(att), "bytes": sz,
                         "est_tokens_per_turn": toks(sz) + docs * 2000,
                         "largest": sorted(
                             [{"name": x.get("filename"), "kb": round((x.get("size_bytes") or 0)/1024)}
                              for x in att], key=lambda z: -z["kb"])[:6]}
    sys.stderr.write(f"[5/5] {len(projects)} projects\n")

    # --- derived ------------------------------------------------------------
    g = usage
    tin = g.get("uncached_input", 0) + g.get("cache_read", 0) + g.get("cache_write", 0)
    reqs = g.get("requests", 0) or 0
    prem = sum(v for k, v in by_model.items() if is_premium(k))
    agentic = sum(v for k, v in by_product.items() if any(p in k for p in AGENTIC))
    gaps = [d["median_gap_s"] for d in deep if d["assistant_turns"] > 3]
    long_ctx = sum(v for k, v in by_ctx.items() if is_long_context(k))
    n_long_lived = sum(1 for h in spans if h > LONG_LIVED_SPAN_HOURS)

    flags = []
    if dup_rate > 0.5: flags.append("NO_MEMOISATION")
    if projects and max(p["est_tokens_per_turn"] for p in projects.values()) > 100_000:
        flags.append("FAT_PROJECT")
    if cost and agentic / cost > 0.7: flags.append("AGENTIC")
    if cost and prem / cost > 0.6: flags.append("PREMIUM_HEAVY")
    if gaps and sorted(gaps)[len(gaps)//2] <= 2: flags.append("NO_HUMAN_IN_LOOP")
    if kinds.get("scheduled", 0) >= 5: flags.append("UNATTENDED_SCHEDULES")
    if reqs and tin / reqs > 200_000: flags.append("HUGE_CONTEXT")
    if total_tools and errors / max(1, total_tools) > 0.03: flags.append("HIGH_ERROR_RATE")
    browser = sum(v for k, v in tools.items()
                  if k in ("browser_batch", "computer", "navigate", "get_page_text"))
    if total_tools and browser / total_tools > 0.15: flags.append("BROWSER_DRIVING")
    if any("sleep" in i for (n, i) in inputs if n == "bash"): flags.append("SLEEP_POLLING")
    if cost and long_ctx / cost > 0.5: flags.append("LONG_CONTEXT")
    if n_long_lived >= 3: flags.append("LONG_LIVED_SESSIONS")
    if any(d["stale_resumes"] for d in deep): flags.append("STALE_RESUME")

    out = {
        "user": {"email": a.email, "name": user.get("full_name"), "id": uid,
                 "role": user.get("organization_role"), "org": org.get("name")},
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": a.days, "data_refreshed_at": refreshed},
        "cost": {"total_usd": round(cost, 2),
                 "per_day": round(cost / max(1, a.days), 2),
                 "annualised": round(cost / max(1, a.days) * 365, 2),
                 "by_model": {k: round(v, 2) for k, v in sorted(by_model.items(), key=lambda x: -x[1])},
                 "by_product": {k: round(v, 2) for k, v in sorted(by_product.items(), key=lambda x: -x[1])},
                 "by_token_type": {k: round(v, 2) for k, v in sorted(by_ttype.items(), key=lambda x: -x[1])},
                 "premium_share": round(prem / cost, 3) if cost else 0,
                 "agentic_share": round(agentic / cost, 3) if cost else 0,
                 "by_context_window": {k: round(v, 2) for k, v in sorted(by_ctx.items(), key=lambda x: -x[1])},
                 "long_context_share": round(long_ctx / cost, 3) if cost else 0,
                 "per_request": round(cost / reqs, 4) if reqs else 0},
        "usage": {k: int(v) for k, v in g.items()},
        "derived": {"tokens_in_per_request": round(tin / reqs) if reqs else 0,
                    "in_out_ratio": round(tin / g["output"], 1) if g.get("output") else 0,
                    "cache_hit_rate": round(g.get("cache_read", 0) / tin, 3) if tin else 0,
                    "median_gap_s": sorted(gaps)[len(gaps)//2] if gaps else None},
        "activity": {"chats": len(chats), "local_sessions": len(sessions),
                     "sessions_scanned": min(len(sessions), a.scan_sessions),
                     "sessions_deep_read": len(deep),
                     "triggers": dict(kinds),
                     "recurring_jobs": {k: {"days_seen": len(v), "dates": sorted(v)[-6:]}
                                        for k, v in sorted(recurring.items(),
                                                           key=lambda x: -len(x[1]))},
                     "by_hour_utc": dict(sorted(byhour.items())),
                     "sessions_open_over_24h": n_long_lived,
                     "sessions_open_over_7d": sum(1 for h in spans if h > 168),
                     "longest_session_span_h": round(spans[0], 1) if spans else 0},
        "tool_errors_by_tool": sorted(
            [{"tool": shorten(n), "connector": connector_of(n),
              "calls": calls_by_tool.get(n, 0), "errors": sum(k.values()),
              "error_rate": round(sum(k.values()) / calls_by_tool[n], 3)
                            if calls_by_tool.get(n) else None,
              "kinds": dict(k)} for n, k in errors_by_tool.items()],
            key=lambda x: -x["errors"])[:20],
        "duplicates": {
            "within_session": sum(dup_within.values()),
            "across_sessions": sum(dup_across.values()),
            "within_session_share_of_tool_calls":
                round(sum(dup_within.values()) / total_tools, 3) if total_tools else 0,
            "within_session_by_tool": [
                {"tool": shorten(n), "connector": connector_of(n), "repeats": c}
                for n, c in dup_within.most_common(12)],
        },
        "determinism": {"tool_calls_sampled": total_tools,
                        "tool_errors": errors,
                        "error_rate": round(errors / total_tools, 3) if total_tools else 0,
                        "retrieval_calls": len(retr_calls),
                        "unique_retrieval_calls": len(set(retr_calls)),
                        "duplicate_retrieval_rate": round(dup_rate, 3),
                        "top_tools": dict(tools.most_common(15)),
                        "top_tool_pairs": {f"{x}->{y}": c for (x, y), c in pairs.most_common(12)},
                        "most_repeated_inputs": [
                            {"tool": n, "count": c, "input": i[:160]}
                            for (n, i), c in inputs.most_common(15) if c > 2]},
        "projects": projects,
        "largest_sessions": sorted(deep, key=lambda d: -d["messages"])[:6],
        # Context sizes rank sessions, they don't price them: tool results are
        # read truncated (understates) and compaction is invisible (overstates,
        # hence the 1M cap). Long-context dollars come from Analytics above.
        "long_sessions": sorted([d for d in deep if d["peak_context_tokens"] >= LONG_CONTEXT_TOKENS
                                 or d["span_hours"] > LONG_LIVED_SPAN_HOURS],
                                key=lambda d: -d["peak_context_tokens"])[:6],
        "sample_prompts": prompts[:10],
        "flags": flags,
    }
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, a.email.split("@")[0].replace(".", "-") + ".json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    sys.stderr.write(f"\nwrote {path}\n")
    print(json.dumps({"email": a.email, "name": user.get("full_name"),
                      "cost": round(cost, 2), "flags": flags, "path": path}))


if __name__ == "__main__":
    main()
