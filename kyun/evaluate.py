"""Score Kyun and the flat-search baseline on a hand-labelled question set."""
from __future__ import annotations

import json
import time

from .qa.baseline import FlatSearch
from .qa.engine import WhyEngine


def _decisions_for_chunk(g, chunk_id: str) -> set[str]:
    if chunk_id.startswith("commit:"):
        sha = chunk_id.split(":", 1)[1]
        out = {d for d in g.by_label("Decision") if sha in g.node(d).get("shas", [])}
        return out
    if chunk_id.startswith("rn:"):
        return {d for d, _, _ in g.inc(chunk_id, "DOCUMENTED_IN")}
    return set()


def run_eval(g, spec: dict) -> dict:
    eng = WhyEngine(g)
    base = FlatSearch(g)
    rows = []
    for item in spec["questions"]:
        t0 = time.time()
        a = eng.ask(item["q"])
        ms = round((time.time() - t0) * 1000)
        live = a.current["id"] if a.current else None
        sup = {s["id"] for s in a.superseded}
        checks = {}
        if item["status"] != "any":
            checks["status"] = a.status == item["status"]
        if item.get("live") and item["status"] != "not_in_graph":
            checks["right decision"] = live in item["live"]
        if item.get("superseded"):
            checks["flags the overridden decision"] = set(item["superseded"]) <= sup
        if item.get("gold_note"):
            versions, phrase = item["gold_note"][0].split("|"), item["gold_note"][1].lower()
            cur = a.current or {}
            blob = " ".join([cur.get("title", "")] + [r["text"] for r in cur.get("rationale", [])]).lower()
            hit = any(n["version"] in versions and phrase in n["text"].lower() for n in cur.get("notes", []))
            hit = hit or (phrase in blob and any(n["version"] in versions for n in cur.get("notes", [])))
            checks["right evidence"] = hit
        if item.get("people"):
            checks["right person"] = bool(a.ask) and a.ask[0]["name"] in item["people"] and a.ask[0]["active"]
        if item.get("headline_contains"):
            checks["catches the premise"] = item["headline_contains"] in a.headline
        ok = all(checks.values())

        b_row = None
        if item.get("baseline", True):
            hits = base.search(item["q"], k=3)
            top = hits[0] if hits else None
            top_dec = _decisions_for_chunk(g, top["id"]) if top else set()
            if item.get("gold_note") and top:
                versions, phrase = item["gold_note"][0].split("|"), item["gold_note"][1].lower()
                notes = [top["id"]] if top["id"].startswith("rn:") else \
                        [rn for d in top_dec for rn, _, _ in g.out(d, "DOCUMENTED_IN")]
                b_ok = any(g.node(rn)["version"] in versions and phrase in g.node(rn)["text"].lower() for rn in notes)
                b_row = {"ok": b_ok, "note": "right evidence" if b_ok else "wrong evidence", "top": top}
            elif item["status"] == "not_in_graph":
                b_ok = top is None
                b_note = "returned an answer anyway" if top else "returned nothing"
            else:
                b_ok = bool(top_dec & set(item.get("live", [])))
                stale = bool(top_dec & set(item.get("superseded", [])))
                b_note = ("quoted the overridden decision" if stale else
                          "right decision" if b_ok else "wrong evidence")
            if not (item.get("gold_note") and top):
                b_row = {"ok": b_ok, "note": b_note, "top": top}
        rows.append({"id": item["id"], "category": item["category"], "q": item["q"], "ok": ok,
                     "checks": checks, "status": a.status, "headline": a.headline,
                     "confidence": a.confidence, "live": live, "ms": ms, "baseline": b_row})

    def pct(xs):
        xs = list(xs)
        return round(100 * sum(xs) / len(xs), 1) if xs else None

    k_all = [r["ok"] for r in rows]
    b_all = [r["baseline"]["ok"] for r in rows if r["baseline"]]
    k_same = [r["ok"] for r in rows if r["baseline"]]
    refusal = [r for r in rows if spec_item(spec, r["id"])["status"] == "not_in_graph"]
    sup_rows = [r for r in rows if spec_item(spec, r["id"]).get("superseded")]
    summary = {
        "questions": len(rows),
        "kyun_correct_pct": pct(k_all),
        "kyun_correct_on_baseline_set_pct": pct(k_same),
        "baseline_correct_pct": pct(b_all),
        "refusals_correct": f"{sum(r['ok'] for r in refusal)}/{len(refusal)}",
        "baseline_answered_unanswerable": f"{sum(1 for r in refusal if r['baseline'] and not r['baseline']['ok'])}/{len(refusal)}",
        "supersession_flagged": f"{sum(r['checks'].get('flags the overridden decision', False) for r in sup_rows)}/{len(sup_rows)}",
        "baseline_quoted_overridden": f"{sum(1 for r in sup_rows if r['baseline'] and r['baseline']['note'] == 'quoted the overridden decision')}/{len(sup_rows)}",
        "median_ms": sorted(r["ms"] for r in rows)[len(rows) // 2],
    }
    return {"repo": spec["repo"], "summary": summary, "rows": rows}


def spec_item(spec, qid):
    return next(i for i in spec["questions"] if i["id"] == qid)


def to_markdown(result: dict) -> str:
    s = result["summary"]
    lines = [f"# Kyun evaluation on {result['repo']}", "",
             f"- Questions: {s['questions']}",
             f"- Kyun correct: **{s['kyun_correct_pct']}%**",
             f"- Flat search baseline correct (same data, no graph): **{s['baseline_correct_pct']}%** "
             f"(Kyun on the same subset: {s['kyun_correct_on_baseline_set_pct']}%)",
             f"- Refused unanswerable questions: {s['refusals_correct']} (baseline answered {s['baseline_answered_unanswerable']} of them anyway)",
             f"- Overridden decisions flagged: {s['supersession_flagged']} (baseline quoted the overridden one in {s['baseline_quoted_overridden']})",
             f"- Median answer time: {s['median_ms']} ms", "",
             "| # | Category | Question | Kyun | Kyun status | Baseline |", "|---|---|---|---|---|---|"]
    for r in result["rows"]:
        b = r["baseline"]
        bt = "n/a" if not b else ("✅ " if b["ok"] else "❌ ") + b["note"]
        failed = [k for k, v in r["checks"].items() if not v]
        kt = "✅" if r["ok"] else "❌ " + ", ".join(failed)
        lines.append(f"| {r['id']} | {r['category']} | {r['q']} | {kt} | {r['status']} ({r['confidence']}) | {bt} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import sys
    from .graph.store import MemoryGraph
    g = MemoryGraph.load(sys.argv[1])
    spec = json.load(open(sys.argv[2], encoding="utf-8"))
    res = run_eval(g, spec)
    print(to_markdown(res))
