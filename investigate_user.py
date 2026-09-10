#!/usr/bin/env python3
"""
Single-user spend investigation.

Points the hypothesis tree (H1-H6 in readme.md) at ONE named user and reports
which mechanism is actually driving their bill.

    python3 investigate_user.py someone@example.com --days 30

Stdlib only. Reads keys from .env.

WHAT THIS READS
  Analytics API   -> actual USD cost, by model and product. No content.
  Compliance API  -> chat + session metadata, and (with --transcripts)
                     message content in order to reconstruct token counts.

Neither API gives per-chat cost: Analytics has cost but no content, Compliance
has content but no cost (confirmed in Anthropic's own docs for BOTH chats and
sessions). So per-chat cost here is RECONSTRUCTED from transcript shape and
then reconciled against the Analytics actual. Treat the reconstruction as a
ranking of mechanisms, not as an invoice.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

API = "https://api.anthropic.com"
VERSION = "2023-06-01"

# ---------------------------------------------------------------- pricing ---
# List price per million tokens (input, output). Used only for counterfactuals
# -- the headline cost figure comes from the Analytics API, not from these.
PRICING = {
    "claude-opus-4":    (15.00, 75.00),
    "claude-opus-5":    (15.00, 75.00),
    "claude-sonnet-4":  (3.00,  15.00),
    "claude-sonnet-5":  (3.00,  15.00),
    "claude-haiku-4-5": (1.00,   5.00),
    "claude-haiku-4":   (0.80,   4.00),
}
DEFAULT_PRICE = (3.00, 15.00)          # unknown model -> assume Sonnet-class
CACHE_READ_DISCOUNT = 0.10             # cached input costs ~10% of full price
CHARS_PER_TOKEN = 3.9                  # readme's heuristic; ranking-grade only
CACHE_TTL_SECONDS = 5 * 60


def price_for(model):
    if not model:
        return DEFAULT_PRICE
    m = model.lower()
    for key, val in PRICING.items():
        if key in m:
            return val
    if "opus" in m:
        return PRICING["claude-opus-5"]
    if "haiku" in m:
        return PRICING["claude-haiku-4-5"]
    if "fable" in m or "mythos" in m:
        return (15.00, 75.00)
    return DEFAULT_PRICE


def is_premium(model):
    m = (model or "").lower()
    return any(t in m for t in ("opus", "fable", "mythos"))


# ------------------------------------------------------------------- http ---
class Http:
    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.calls = 0

    def get(self, path, params=None, retries=5):
        url = API + path
        if params:
            # bracket-notation list params must repeat, not be JSON-encoded
            pairs = []
            for k, v in params.items():
                if v is None:
                    continue
                if isinstance(v, (list, tuple)):
                    pairs.extend((k, str(item)) for item in v)
                else:
                    pairs.append((k, str(v)))
            url += "?" + urllib.parse.urlencode(pairs)

        for attempt in range(retries):
            req = urllib.request.Request(url, headers={
                "x-api-key": self.key,
                "anthropic-version": VERSION,
                "accept": "application/json",
            })
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                body = e.read().decode()[:500]
                if e.code in (429, 500, 502, 503, 529) and attempt < retries - 1:
                    wait = min(2 ** attempt, 30)
                    print(f"    {e.code} on {self.label}, retry in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise SystemExit(
                    f"\n{self.label} API error {e.code} on {path}\n{body}\n"
                    + _hint(e.code, self.label)
                )
            except urllib.error.URLError as e:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise SystemExit(f"\nNetwork error calling {path}: {e}")


def _hint(code, label):
    if code == 401:
        return f"-> The {label} key was rejected. Check the key in .env."
    if code == 403:
        return (f"-> The {label} key lacks the required scope.\n"
                "   Compliance needs read:compliance_user_data AND read:compliance_org_data.\n"
                "   An Admin key (sk-ant-admin01-) will NOT work for compliance endpoints.")
    if code == 404:
        return "-> Endpoint not available to this org (may need Enterprise / a feature flag)."
    if code == 400:
        return "-> Check the date range: cost data older than ~30d is revised; future dates 400."
    return ""


def _can_read_analytics(key):
    """Cheap probe: does this key actually reach the Analytics cost endpoint?"""
    if not key:
        return False
    url = (API + "/v1/organizations/analytics/cost_report"
           "?starting_at=2026-01-01T00:00:00Z&bucket_width=1d&limit=1")
    req = urllib.request.Request(url, headers={
        "x-api-key": key, "anthropic-version": VERSION, "accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60):
            return True
    except urllib.error.HTTPError as e:
        return e.code not in (401, 403)
    except urllib.error.URLError:
        return False


def load_env(path=".env"):
    if not os.path.exists(path):
        raise SystemExit(f"No {path} found. Copy .env.sample and fill it in.")
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# ------------------------------------------------------------- pagination ---
def page_walk(http, path, params, max_pages=200):
    """For endpoints using the opaque page/next_page token scheme."""
    params = dict(params or {})
    for _ in range(max_pages):
        resp = http.get(path, params)
        for row in resp.get("data", []):
            yield row
        nxt = resp.get("next_page")
        if not nxt:
            return
        params["page"] = nxt


def cursor_walk(http, path, params, max_pages=200):
    """For chat endpoints using first_id/last_id/has_more cursors."""
    params = dict(params or {})
    for _ in range(max_pages):
        resp = http.get(path, params)
        rows = resp.get("data", [])
        for row in rows:
            yield row
        if not resp.get("has_more") or not resp.get("last_id"):
            return
        params["after_id"] = resp["last_id"]


# ------------------------------------------------------------- resolution ---
def resolve_user(comp, email):
    """Email -> user_id. Requires walking each linked org's user list."""
    orgs = list(page_walk(comp, "/v1/compliance/organizations", {"limit": 100}))
    if not orgs:
        raise SystemExit("No organizations returned. Check compliance key scopes.")
    target = email.lower().strip()
    for org in orgs:
        uuid = org.get("uuid")
        for u in page_walk(comp,
                           f"/v1/compliance/organizations/{uuid}/users",
                           {"limit": 500}):
            if (u.get("email") or "").lower() == target:
                return u, org
    raise SystemExit(
        f"\n{email} not found in any linked organization.\n"
        "Users are dropped from this list the moment they leave the org.\n"
        f"Searched {len(orgs)} org(s)."
    )


# ------------------------------------------------------------------- cost ---
def cents_to_dollars(s):
    try:
        return float(s) / 100.0
    except (TypeError, ValueError):
        return 0.0


def fetch_cost(analytics, user_id, start, end):
    """Actual USD from the Analytics API, grouped by model and product."""
    rows = []
    params = {
        "starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket_width": "1d",
        "group_by[]": ["model", "product", "token_type"],
        "user_ids[]": [user_id],
        "limit": 31,
    }
    resp = analytics.get("/v1/organizations/analytics/cost_report", params)
    refreshed = resp.get("data_refreshed_at")
    for bucket in resp.get("data", []):
        for r in bucket.get("results", []):
            rows.append({
                "date": bucket.get("starting_at", "")[:10],
                "amount": cents_to_dollars(r.get("amount")),
                "model": r.get("model"),
                "product": r.get("product"),
                "token_type": r.get("token_type"),
                "requests": r.get("requests") or 0,
            })
    while resp.get("has_more") and resp.get("next_page"):
        params["page"] = resp["next_page"]
        resp = analytics.get("/v1/organizations/analytics/cost_report", params)
        for bucket in resp.get("data", []):
            for r in bucket.get("results", []):
                rows.append({
                    "date": bucket.get("starting_at", "")[:10],
                    "amount": cents_to_dollars(r.get("amount")),
                    "model": r.get("model"),
                    "product": r.get("product"),
                    "token_type": r.get("token_type"),
                    "requests": r.get("requests") or 0,
                })
    return rows, refreshed


def fetch_usage(analytics, user_id, start, end):
    """Real token counts from the Analytics API -- the ground truth for H1."""
    params = {
        "starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket_width": "1d",
        "group_by[]": ["model", "product"],
        "user_ids[]": [user_id],
        "limit": 31,
    }
    agg = defaultdict(float)
    by_product = defaultdict(lambda: defaultdict(float))
    resp = analytics.get("/v1/organizations/analytics/usage_report", params)
    while True:
        for bucket in resp.get("data", []):
            for r in bucket.get("results", []):
                cc = r.get("cache_creation") or {}
                vals = {
                    "uncached_input": r.get("uncached_input_tokens") or 0,
                    "output": r.get("output_tokens") or 0,
                    "cache_read": r.get("cache_read_input_tokens") or 0,
                    "cache_write": (cc.get("ephemeral_5m_input_tokens") or 0)
                                   + (cc.get("ephemeral_1h_input_tokens") or 0),
                    "requests": r.get("requests") or 0,
                }
                for k, v in vals.items():
                    agg[k] += v
                    by_product[r.get("product") or "unknown"][k] += v
        if resp.get("has_more") and resp.get("next_page"):
            params["page"] = resp["next_page"]
            resp = analytics.get("/v1/organizations/analytics/usage_report", params)
        else:
            break
    return dict(agg), {k: dict(v) for k, v in by_product.items()}


# --------------------------------------------------------------- contents ---
def text_len(content):
    """Character count of the text actually present in a content array."""
    n = 0
    for block in content or []:
        t = block.get("type")
        if t == "text":
            n += len(block.get("text") or "")
        elif t == "tool_use":
            inp = block.get("input")
            n += len(inp if isinstance(inp, str) else json.dumps(inp or {}))
            n += len(block.get("name") or "")
        elif t == "tool_result":
            for entry in block.get("content") or []:
                if entry.get("type") == "text":
                    n += len(entry.get("text") or "")
    return n


def toks(chars):
    return int(chars / CHARS_PER_TOKEN)


def analyze_turns(messages, model_hint=None):
    """
    Core cost reconstruction. Walks a transcript in order and charges each
    assistant turn for the full context that preceded it -- the quadratic in
    readme.md H1 -- discounting the prefix that a warm 5-minute cache covers.
    """
    running_tokens = 0            # content tokens accumulated so far
    billed_full = 0               # input tokens billed at full price
    billed_cached = 0             # input tokens billed at cache-read price
    output_tokens = 0
    content_tokens = 0
    assistant_turns = 0
    cold_resumes = 0
    tool_calls = 0
    gaps = []
    models = defaultdict(int)
    last_assistant_ts = None      # end of the PREVIOUS assistant turn

    for msg in messages:
        content = msg.get("content") or []
        role = msg.get("role")
        chars = text_len(content)
        t = toks(chars)
        content_tokens += t

        for b in content:
            if b.get("type") == "tool_use":
                tool_calls += 1

        ts = parse_ts(msg.get("created_at"))

        if role == "assistant":
            assistant_turns += 1
            if msg.get("model"):
                models[msg["model"]] += 1

            # The cache-killing gap is previous RESPONSE -> this turn, i.e.
            # human think time. User and assistant messages in the same turn
            # share a timestamp, so measuring from the immediately preceding
            # message would always read ~0 and never detect a cold resume.
            if ts and last_assistant_ts:
                gap = (ts - last_assistant_ts).total_seconds()
                gaps.append(gap)
                warm = gap < CACHE_TTL_SECONDS
            else:
                warm = False      # first turn of a thread is always cold
            if not warm:
                cold_resumes += 1

            # The context that has to be re-sent for this turn.
            if warm:
                billed_cached += running_tokens
            else:
                billed_full += running_tokens

            output_tokens += t
            if ts:
                last_assistant_ts = ts
        running_tokens += t

    model = max(models, key=models.get) if models else model_hint
    billed_input = billed_full + billed_cached
    return {
        "assistant_turns": assistant_turns,
        "total_messages": len(messages),
        "content_tokens": content_tokens,
        "billed_input_tokens": billed_input,
        "billed_full_tokens": billed_full,
        "billed_cached_tokens": billed_cached,
        "output_tokens": output_tokens,
        "repayment_multiplier": round(billed_input / content_tokens, 2) if content_tokens else 0,
        "cold_resumes": cold_resumes,
        "cold_resume_rate": round(cold_resumes / assistant_turns, 2) if assistant_turns else 0,
        "tool_calls": tool_calls,
        "median_gap_s": int(sorted(gaps)[len(gaps) // 2]) if gaps else 0,
        "model": model,
    }


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def estimate_cost(stats, model=None, project_tokens=0):
    """
    Reconstructed $ for one conversation, incl. re-sent project attachments.

    Project attachments sit at the head of every request, so they are the part
    of the prefix a warm cache actually covers: they are charged at full price
    on cold turns and at the cache-read rate on warm ones. Charging them at
    full price on every turn overstates H2 by ~3x.
    """
    model = model or stats.get("model")
    inp, outp = price_for(model)
    turns = stats["assistant_turns"]
    cold = stats["cold_resumes"]
    warm_turns = max(0, turns - cold)

    full = stats["billed_full_tokens"] + project_tokens * cold
    cached = stats["billed_cached_tokens"] + project_tokens * warm_turns
    return (
        full / 1e6 * inp
        + cached / 1e6 * inp * CACHE_READ_DISCOUNT
        + stats["output_tokens"] / 1e6 * outp
    )


# ------------------------------------------------------------ diagnostics ---
def counterfactuals(stats, model, project_tokens):
    """What each lever in the hypothesis tree would have saved."""
    base = estimate_cost(stats, model, project_tokens)
    out = {"actual_reconstructed": base}

    # H1: same content, split so no thread exceeds 8 assistant turns.
    turns = stats["assistant_turns"]
    if turns > 8:
        chunks = max(1, -(-turns // 8))
        per_turns = turns / chunks
        per = dict(stats)
        per["assistant_turns"] = per_turns
        # Re-payment within one chunk falls with the square of its turn count;
        # total across all chunks therefore falls ~linearly with `chunks`.
        scale = (1.0 / chunks) ** 2
        per["billed_full_tokens"] = stats["billed_full_tokens"] * scale
        per["billed_cached_tokens"] = stats["billed_cached_tokens"] * scale
        per["output_tokens"] = stats["output_tokens"] / chunks
        # Per-turn counts must be divided too, or the project attachment and
        # cache accounting get charged `chunks` times over.
        per["cold_resumes"] = stats["cold_resumes"] / chunks
        out["if_split_every_8_turns"] = estimate_cost(per, model, project_tokens) * chunks

    # H3: identical conversation served by Sonnet.
    if is_premium(model):
        out["if_sonnet"] = estimate_cost(stats, "claude-sonnet-5", project_tokens)

    # H2: project attachments trimmed by 80%.
    if project_tokens:
        out["if_project_trimmed_80pct"] = estimate_cost(
            stats, model, int(project_tokens * 0.2))

    # H4: every turn lands inside a warm cache window.
    if stats["billed_full_tokens"]:
        warm = dict(stats)
        warm["billed_cached_tokens"] = stats["billed_input_tokens"]
        warm["billed_full_tokens"] = 0
        out["if_cache_always_warm"] = estimate_cost(warm, model, project_tokens)

    return out


def flags_for(stats, project_tokens, model):
    f = []
    if stats["repayment_multiplier"] > 15:
        f.append("CONTEXT_REPAYMENT")
    if project_tokens > 50_000:
        f.append("FAT_PROJECT")
    if is_premium(model) and stats["assistant_turns"] <= 3 and stats["content_tokens"] < 4000:
        f.append("PREMIUM_ON_TRIVIAL")
    if stats["cold_resume_rate"] > 0.7 and stats["assistant_turns"] > 3:
        f.append("COLD_CACHE")
    if stats["tool_calls"] > 50:
        f.append("AGENTIC_FANOUT")
    return f


def money(x):
    return f"${x:,.2f}"


def bar(frac, width=28):
    n = max(0, min(width, int(round(frac * width))))
    return "█" * n + "·" * (width - n)


# ------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser(description="Investigate one user's Claude spend.")
    ap.add_argument("email")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--transcripts", action="store_true",
                    help="Pull message content to reconstruct per-chat cost (tier 2).")
    ap.add_argument("--max-chats", type=int, default=40)
    ap.add_argument("--max-sessions", type=int, default=40)
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    env = load_env()
    if not env.get("ANTHROPIC_COMPLIANCE_KEY"):
        raise SystemExit("ANTHROPIC_COMPLIANCE_KEY missing from .env")

    comp = Http(env["ANTHROPIC_COMPLIANCE_KEY"], "Compliance")

    # The Enterprise Analytics endpoints need a key created at
    # claude.ai > Organization settings > API. A plain workspace key
    # (sk-ant-api03-) is rejected with "x-api-key header is required", which
    # is a misleading 401. Probe the configured analytics key and fall back to
    # the compliance key, which in practice often carries both scopes.
    analytics_key = env.get("ANTHROPIC_ANALYTICS_KEY") or ""
    analytics = Http(analytics_key, "Analytics") if analytics_key else None
    if not analytics or not _can_read_analytics(analytics_key):
        if _can_read_analytics(env["ANTHROPIC_COMPLIANCE_KEY"]):
            if analytics_key:
                print("      note: ANTHROPIC_ANALYTICS_KEY is not valid for the")
                print("      Analytics API; using the compliance key, which is.")
            analytics = Http(env["ANTHROPIC_COMPLIANCE_KEY"], "Analytics")
        else:
            raise SystemExit(
                "\nNeither key can read /v1/organizations/analytics/cost_report.\n"
                "Create an Analytics API key as Primary Owner at\n"
                "  claude.ai > Organization settings > API\n"
                "and put it in .env as ANTHROPIC_ANALYTICS_KEY.\n"
                "A workspace key (sk-ant-api03-) will not work.")

    # Cost data is revised for 30 days; end the window 2 days back so we are
    # not reading provisional numbers.
    end = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    start = end - timedelta(days=args.days)

    print(f"\n{'=' * 66}")
    print(f"  SPEND INVESTIGATION — {args.email}")
    print(f"  Window: {start:%Y-%m-%d} to {end:%Y-%m-%d}  ({args.days}d, ending 2d back)")
    print(f"{'=' * 66}\n")

    print("[1/5] Resolving user...")
    user, org = resolve_user(comp, args.email)
    uid = user["id"]
    print(f"      {user.get('full_name')}  {uid}")
    print(f"      org: {org.get('name')}  role: {user.get('organization_role')}")

    print("\n[2/5] Analytics: actual cost and tokens...")
    cost_rows, refreshed = fetch_cost(analytics, uid, start, end)
    usage, usage_by_product = fetch_usage(analytics, uid, start, end)
    actual = sum(r["amount"] for r in cost_rows)

    if not cost_rows:
        print("      No cost rows returned.")
        print("      Either the user had no billable usage in this window, or the")
        print("      org is on a seat-based contract (cost endpoints then reflect")
        print("      usage credits only). Verify before reading anything below.")

    by_model = defaultdict(float)
    by_product = defaultdict(float)
    by_token_type = defaultdict(float)
    for r in cost_rows:
        by_model[r["model"] or "unknown"] += r["amount"]
        by_product[r["product"] or "unknown"] += r["amount"]
        by_token_type[r["token_type"] or "other"] += r["amount"]

    print(f"      Actual cost: {money(actual)}   (refreshed {refreshed})")

    print("\n[3/5] Compliance: chats and sessions...")
    chats = [c for c in cursor_walk(comp, "/v1/compliance/apps/chats", {
        "user_ids[]": [uid],
        "created_at.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 100,
    })]
    # The LOCAL session list has no user filter -- filter client-side.
    sessions = [s for s in page_walk(comp, "/v1/compliance/apps/sessions/local", {
        "created_at.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 500,
    }) if (s.get("user") or {}).get("id") == uid]
    remote = [s for s in page_walk(comp, "/v1/compliance/apps/sessions/remote", {
        "user_ids[]": [uid],
        "created_at.gte": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 500,
    })]
    print(f"      {len(chats)} chats, {len(sessions)} local sessions, "
          f"{len(remote)} remote sessions")

    surfaces = defaultdict(int)
    for s in sessions:
        surfaces[s.get("product_surface") or "unknown"] += 1

    print("\n[4/5] Projects and attachment weight...")
    project_ids = {c.get("project_id") for c in chats if c.get("project_id")}
    project_ids |= {s.get("claude_project_id") for s in remote if s.get("claude_project_id")}
    projects = {}
    for pid in project_ids:
        att = list(page_walk(comp, f"/v1/compliance/apps/projects/{pid}/attachments",
                             {"limit": 100}))
        # project_doc entries carry no size_bytes; approximate them.
        size = sum(a.get("size_bytes") or 0 for a in att)
        docs = sum(1 for a in att if a.get("type") == "project_doc")
        est_tokens = toks(size) + docs * 2000
        projects[pid] = {
            "attachments": len(att),
            "size_bytes": size,
            "docs_without_size": docs,
            "est_tokens": est_tokens,
        }
        print(f"      {pid[:32]}  {len(att):>3} files  ~{est_tokens:>8,} tok/turn")
    if not projects:
        print("      No projects in play.")

    per_chat = []
    if args.transcripts:
        print(f"\n[5/5] Transcripts (tier 2) — up to {args.max_chats} chats, "
              f"{args.max_sessions} sessions...")
        for c in sorted(chats, key=lambda x: x.get("updated_at") or "",
                        reverse=True)[:args.max_chats]:
            if c.get("deleted_at"):
                continue
            msgs = list(page_walk(comp,
                                  f"/v1/compliance/apps/chats/{c['id']}/messages",
                                  {"limit": 500}))
            if not msgs:
                # unpaginated shape: messages live under chat_messages
                resp = comp.get(f"/v1/compliance/apps/chats/{c['id']}/messages")
                msgs = resp.get("chat_messages", [])
            if not msgs:
                continue
            st = analyze_turns(msgs, c.get("model"))
            ptok = projects.get(c.get("project_id"), {}).get("est_tokens", 0)
            model = st["model"] or c.get("model")
            per_chat.append({
                "kind": "chat",
                "id": c["id"],
                "title": c.get("name") or "(untitled)",
                "model": model,
                "project_id": c.get("project_id"),
                "project_tokens": ptok,
                **st,
                "est_cost": estimate_cost(st, model, ptok),
                "flags": flags_for(st, ptok, model),
                "counterfactuals": counterfactuals(st, model, ptok),
            })
            print(f"      chat {len(per_chat):>3}/{min(len(chats), args.max_chats)}  "
                  f"{st['assistant_turns']:>3} turns  x{st['repayment_multiplier']:>6}  "
                  f"{c.get('name', '')[:34]}")

        for s in sorted(sessions, key=lambda x: x.get("updated_at") or "",
                        reverse=True)[:args.max_sessions]:
            msgs = list(page_walk(comp,
                                  f"/v1/compliance/apps/sessions/local/{s['id']}/messages",
                                  {"limit": 500, "tool_result_max_bytes": 10000}))
            if not msgs:
                continue
            st = analyze_turns(msgs)
            model = st["model"]
            per_chat.append({
                "kind": s.get("product_surface") or "session",
                "id": s["id"],
                "title": f"[{s.get('product_surface')}] session",
                "model": model,
                "project_id": None,
                "project_tokens": 0,
                **st,
                "est_cost": estimate_cost(st, model, 0),
                "flags": flags_for(st, 0, model),
                "counterfactuals": counterfactuals(st, model, 0),
            })
            print(f"      sess {len(per_chat):>3}     {st['assistant_turns']:>3} turns  "
                  f"x{st['repayment_multiplier']:>6}  {st['tool_calls']} tool calls")
    else:
        print("\n[5/5] Skipping transcripts (metadata only). Add --transcripts for tier 2.")

    render(args, user, org, actual, usage, usage_by_product, by_model, by_product,
           by_token_type, chats, sessions, remote, surfaces, projects, per_chat,
           refreshed, start, end)

    os.makedirs(args.out, exist_ok=True)
    payload = {
        "user": {"email": args.email, "id": uid,
                 "name": user.get("full_name"),
                 "role": user.get("organization_role"),
                 "org": org.get("name")},
        "window": {"start": start.isoformat(), "end": end.isoformat(),
                   "days": args.days, "data_refreshed_at": refreshed},
        "actual_cost_usd": actual,
        "cost_by_model": dict(by_model),
        "cost_by_product": dict(by_product),
        "cost_by_token_type": dict(by_token_type),
        "usage_tokens": usage,
        "usage_by_product": usage_by_product,
        "counts": {"chats": len(chats), "local_sessions": len(sessions),
                   "remote_sessions": len(remote), "surfaces": dict(surfaces)},
        "projects": projects,
        "per_conversation": per_chat,
        "api_calls": {"analytics": analytics.calls, "compliance": comp.calls},
    }
    path = os.path.join(args.out, f"investigate_{args.email.split('@')[0]}.json")
    with open(path, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\nFull detail: {path}")
    if per_chat:
        write_csv(args, per_chat)


def write_csv(args, per_chat):
    import csv
    path = os.path.join(args.out, f"investigate_{args.email.split('@')[0]}_chats.csv")
    cols = ["kind", "id", "title", "model", "assistant_turns", "content_tokens",
            "billed_input_tokens", "output_tokens", "repayment_multiplier",
            "cold_resumes", "cold_resume_rate", "tool_calls", "median_gap_s",
            "project_tokens", "est_cost", "flags"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in sorted(per_chat, key=lambda x: -x["est_cost"]):
            w.writerow([r.get(c) if c != "flags" else "|".join(r["flags"]) for c in cols])
    print(f"Per-conversation CSV: {path}")


def render(args, user, org, actual, usage, usage_by_product, by_model, by_product,
           by_token_type, chats, sessions, remote, surfaces, projects, per_chat,
           refreshed, start, end):
    W = 66
    print(f"\n{'=' * W}")
    print("  FINDINGS")
    print(f"{'=' * W}")

    days = max(1, args.days)
    print(f"\nActual spend (Analytics API)        {money(actual)}")
    print(f"  per day                           {money(actual / days)}")
    print(f"  annualised                        {money(actual / days * 365)}")

    if by_product:
        print("\nWHERE (by product)")
        tot = sum(by_product.values()) or 1
        for k, v in sorted(by_product.items(), key=lambda x: -x[1]):
            print(f"  {k:<22} {money(v):>10}  {bar(v / tot)} {v / tot * 100:4.1f}%")

    if by_model:
        print("\nWHICH MODEL")
        tot = sum(by_model.values()) or 1
        prem = sum(v for k, v in by_model.items() if is_premium(k))
        for k, v in sorted(by_model.items(), key=lambda x: -x[1]):
            tag = " PREMIUM" if is_premium(k) else ""
            print(f"  {k:<22} {money(v):>10}  {bar(v / tot)} {v / tot * 100:4.1f}%{tag}")
        print(f"\n  Premium-model share of spend: {prem / tot * 100:.1f}%")

    if by_token_type:
        print("\nCOST BY TOKEN TYPE  (this is the H1/H4 tell)")
        tot = sum(by_token_type.values()) or 1
        for k, v in sorted(by_token_type.items(), key=lambda x: -x[1]):
            print(f"  {k:<34} {money(v):>10}  {v / tot * 100:5.1f}%")

    if usage:
        inp = usage.get("uncached_input", 0)
        cr = usage.get("cache_read", 0)
        cw = usage.get("cache_write", 0)
        out = usage.get("output", 0)
        reqs = usage.get("requests", 0)
        total_in = inp + cr + cw
        print("\nTOKEN VOLUME (actual, not estimated)")
        print(f"  uncached input   {inp:>14,.0f}")
        print(f"  cache read       {cr:>14,.0f}")
        print(f"  cache write      {cw:>14,.0f}")
        print(f"  output           {out:>14,.0f}")
        print(f"  requests         {reqs:>14,.0f}")
        if reqs:
            print(f"\n  Input tokens per request: {total_in / reqs:>12,.0f}")
            print(f"  Output tokens per request:{out / reqs:>12,.0f}")
        if total_in:
            print(f"  Cache hit rate:           {cr / total_in * 100:>11.1f}%")
        if out:
            print(f"  Input:output ratio:       {total_in / out:>11,.1f} : 1")

    print("\nACTIVITY")
    print(f"  claude.ai chats        {len(chats):>5}")
    print(f"  local sessions         {len(sessions):>5}   {dict(surfaces) or ''}")
    print(f"  remote (Cowork web)    {len(remote):>5}")
    if projects:
        heavy = max(projects.values(), key=lambda p: p["est_tokens"])
        print(f"  projects               {len(projects):>5}   "
              f"heaviest ~{heavy['est_tokens']:,} tok re-sent per turn")

    # ---- verdict -----------------------------------------------------------
    print(f"\n{'=' * W}")
    print("  DIAGNOSIS")
    print(f"{'=' * W}\n")

    verdicts = []
    total_in = (usage.get("uncached_input", 0) + usage.get("cache_read", 0)
                + usage.get("cache_write", 0))
    reqs = usage.get("requests", 0)
    out_t = usage.get("output", 0)

    if reqs and total_in / reqs > 100_000:
        verdicts.append(
            ("H1/H2  CONTEXT WEIGHT", "STRONG",
             f"{total_in / reqs:,.0f} input tokens per request. Each message is "
             f"carrying a very large context. That is re-payment (long threads) "
             f"or a fat project, not hard work."))
    elif reqs and total_in / reqs > 40_000:
        verdicts.append(
            ("H1/H2  CONTEXT WEIGHT", "MODERATE",
             f"{total_in / reqs:,.0f} input tokens per request — heavier than a "
             f"typical chat, worth splitting threads."))

    if total_in and usage.get("cache_read", 0) / total_in < 0.3 and reqs > 20:
        verdicts.append(
            ("H4  COLD CACHE", "PRESENT",
             f"Only {usage.get('cache_read', 0) / total_in * 100:.0f}% of input "
             f"was served from cache. Human think-time gaps exceed the 5-minute "
             f"TTL. Explains cost; not directly actionable."))

    prem_share = (sum(v for k, v in by_model.items() if is_premium(k))
                  / (sum(by_model.values()) or 1))
    if prem_share > 0.6:
        verdicts.append(
            ("H3  PREMIUM MODEL", "STRONG",
             f"{prem_share * 100:.0f}% of spend on premium models. If the work is "
             f"routine, an org default of Sonnet cuts this ~5x. Same-day, reversible."))

    agentic = sum(v for k, v in by_product.items()
                  if k in ("cowork", "claude_code", "office_agent"))
    if actual and agentic / actual > 0.4:
        verdicts.append(
            ("H5  AGENTIC FAN-OUT", "STRONG",
             f"{agentic / actual * 100:.0f}% of spend is agentic surfaces "
             f"(Cowork/Code). Tool loops generate large intermediate volume."))

    if out_t and total_in / out_t > 100:
        verdicts.append(
            ("H1  RE-PAYMENT", "STRONG",
             f"Input:output is {total_in / out_t:,.0f}:1. Overwhelmingly paying to "
             f"re-read context rather than to generate. Classic long-thread signature."))

    if per_chat:
        flat = defaultdict(lambda: {"n": 0, "cost": 0.0})
        for r in per_chat:
            for fl in r["flags"]:
                flat[fl]["n"] += 1
                flat[fl]["cost"] += r["est_cost"]
        if flat:
            print("FLAGGED CONVERSATIONS (reconstructed)")
            for fl, d in sorted(flat.items(), key=lambda x: -x[1]["cost"]):
                print(f"  {fl:<24} {d['n']:>3} convs   ~{money(d['cost'])}")
            print()

        print("TOP CONVERSATIONS BY RECONSTRUCTED COST")
        for r in sorted(per_chat, key=lambda x: -x["est_cost"])[:8]:
            print(f"  ~{money(r['est_cost']):>9}  {r['assistant_turns']:>3}t  "
                  f"x{r['repayment_multiplier']:<6.1f} {r['title'][:38]}")
            if r["flags"]:
                print(f"              {' '.join(r['flags'])}")

        recon = sum(r["est_cost"] for r in per_chat)
        print(f"\nRECONCILIATION")
        print(f"  reconstructed (sampled)  {money(recon)}")
        print(f"  actual (full window)     {money(actual)}")
        ratio = recon / actual if actual else 0
        blown = ratio > 1.4 or ratio < 0.6
        if actual:
            print(f"  ratio                    {ratio:.2f}x")
            if blown:
                print("\n  ⚠ RECONSTRUCTION REJECTED — off by more than 40%.")
                print("    Per-conversation dollar figures above are NOT usable.")
                cache_rate = 0
                tot_in = (usage.get("uncached_input", 0) + usage.get("cache_read", 0)
                          + usage.get("cache_write", 0))
                if tot_in:
                    cache_rate = usage.get("cache_read", 0) / tot_in
                if ratio > 1.4 and cache_rate > 0.7:
                    print(f"    Cause: {cache_rate * 100:.0f}% of input was served from cache.")
                    print("    The quadratic model assumes a human re-paying for context;")
                    print("    in a long agentic tool loop the prefix stays warm, so the")
                    print("    re-payment is billed at the cache-read rate, not full price.")
                    print("    The SHAPE findings (turn counts, tool calls, model mix)")
                    print("    remain valid -- they are measured, not reconstructed.")

        savings = defaultdict(float)
        for r in per_chat:
            cf = r["counterfactuals"]
            base = cf["actual_reconstructed"]
            for lever, val in cf.items():
                if lever != "actual_reconstructed" and val < base:
                    savings[lever] += base - val
        if savings and not blown:
            print("\nSAVINGS BY LEVER (reconstructed, sampled window)")
            for lever, val in sorted(savings.items(), key=lambda x: -x[1]):
                print(f"  {lever:<28} ~{money(val)}")
        elif savings:
            # Scale the levers to the actual bill and show only their relative
            # weight. Absolute reconstructed dollars are meaningless here.
            tot = sum(savings.values()) or 1
            print("\nLEVERS BY RELATIVE WEIGHT (reconstruction rejected, so")
            print("shares of the actual bill -- not reconstructed dollars)")
            for lever, val in sorted(savings.items(), key=lambda x: -x[1]):
                print(f"  {lever:<28} {val / tot * 100:5.1f}% of modelled headroom")

    if not verdicts:
        verdicts.append(
            ("H6  LEGITIMATE", "LIKELY",
             "No mechanism flag fired. Spend looks like real work on appropriate "
             "models in reasonably shaped threads."))

    print("\nRANKED CAUSES")
    for name, strength, why in verdicts:
        print(f"\n  [{strength}] {name}")
        for line in wrap(why, 60):
            print(f"      {line}")

    print(f"\n{'=' * W}")
    if not args.transcripts:
        print("Metadata only. Re-run with --transcripts to attribute cost to")
        print("individual conversations and produce counterfactuals.")
    print()


def wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = (line + " " + w).strip()
    if line:
        out.append(line)
    return out


if __name__ == "__main__":
    main()
