"""Graph-wide views that need no question: knowledge at risk, stale docs."""
from __future__ import annotations

from collections import defaultdict
from datetime import date


def knowledge_at_risk(g, inactive_days: int = 540, min_decisions: int = 4, limit: int = 15) -> list[dict]:
    """Code whose reasons were decided only by people who have gone quiet."""
    today = date.fromisoformat(g.meta.get("last_commit") or date.today().isoformat())
    rows = []
    for sid in g.by_label("Symbol") + g.by_label("ConfigKey"):
        s = g.node(sid)
        if s.get("is_test") or not s.get("alive", True) or s["name"].startswith("__"):
            continue
        decs, people = set(), {}
        for did, _, _ in g.inc(sid, "ABOUT"):
            d = g.node(did)
            if g.lbl(did) != "Decision" or d.get("low_signal"):
                continue
            decs.add(did)
            for pid, _, _ in g.out(did, "DECIDED_BY"):
                p = g.node(pid)
                if not p.get("is_bot"):
                    people[pid] = p
        if len(decs) < min_decisions or not people:
            continue
        active = [p for p in people.values()
                  if (today - date.fromisoformat(p["last_active"])).days <= inactive_days]
        if active:
            continue
        last = max(g.node(d)["date"] for d in decs)
        rows.append({"symbol": f"{s['module']}.{s['name']}", "decisions": len(decs), "last_decision": last,
                     "known_by": sorted((p["name"] for p in people.values()), key=lambda n: n)[:4],
                     "last_seen": max(p["last_active"] for p in people.values())})
    rows.sort(key=lambda r: (-r["decisions"], r["symbol"]))
    return rows[:limit]


def stale_release_notes(g, limit: int = 20) -> list[dict]:
    """Release notes that describe a decision someone later overrode."""
    out = []
    for newer, rel, older, p in g.edges():
        if rel != "SUPERSEDES":
            continue
        if p.get("rule") == "code lifecycle" and g.node(newer).get("kind") not in ("revert", "remove"):
            continue
        for rn, _, _ in g.out(older, "DOCUMENTED_IN"):
            n = g.node(rn)
            if n["date"] > g.node(newer)["date"]:
                continue
            out.append({"version": n["version"], "note": n["text"][:200], "overridden_by": g.node(newer)["title"],
                        "on": g.node(newer)["date"], "rule": p.get("rule"), "evidence": p.get("evidence")})
    out.sort(key=lambda r: r["on"], reverse=True)
    return out[:limit]


def overview(g) -> dict:
    st = g.stats()
    people = [g.node(p) for p in g.by_label("Person") if not g.node(p).get("is_bot")]
    merged = [p for p in people if len(p.get("names", [])) > 1 or len(p.get("emails", [])) > 1]
    files = [g.node(f) for f in g.by_label("File") if g.node(f).get("aliases")]
    rules = defaultdict(int)
    for _, r, _, p in g.edges():
        if r == "SUPERSEDES":
            rules[p.get("rule")] += 1
    with_why = sum(1 for d in g.by_label("Decision") if g.node(d)["rationale"])
    return {"meta": g.meta, "stats": st, "people_merged": len(merged),
            "people_examples": [{"name": p["name"], "names": p["names"], "emails": len(p["emails"])}
                                for p in sorted(merged, key=lambda p: -p["commits"])[:5]],
            "files_with_history_under_other_names": len(files),
            "file_examples": [{"path": f["path"], "aliases": f["aliases"]} for f in files[:5]],
            "supersession_rules": dict(rules), "decisions_with_reason": with_why,
            "decisions": st["nodes"].get("Decision", 0)}
