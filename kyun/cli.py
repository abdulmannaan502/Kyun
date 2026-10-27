"""kyun — ask a codebase why it is the way it is.

  kyun build  PATH_TO_REPO --name owner/repo --out graph.json.gz
  kyun ask    graph.json.gz "Why doesn't requests cache a default SSLContext?"
  kyun serve  graph.json.gz --port 8000
  kyun eval   graph.json.gz eval/questions_requests.json
  kyun risk   graph.json.gz
  kyun push   graph.json.gz --uri neo4j+s://xxxx.databases.neo4j.io --user neo4j --password ...
  kyun pull   --uri ... --user ... --password ... --out graph.json.gz
  kyun export-csv graph.json.gz neo4j_import/
  kyun fetch-github graph.json.gz --cache .kyun/github   (needs GITHUB_TOKEN)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap


def _load(path):
    from .graph.store import MemoryGraph
    return MemoryGraph.load(path)


def _print_answer(a, width=96):
    W = lambda s, ind="  ": print(textwrap.fill(s, width, initial_indent=ind, subsequent_indent=" " * len(ind)))
    tag = {"answered": "ANSWER", "what_not_why": "NO REASON RECORDED", "not_in_graph": "NOT IN THE GRAPH"}[a.status]
    print(f"\n{tag}  ·  confidence {a.confidence:.2f} ({a.confidence_label})")
    W(a.headline)
    if a.why:
        print("\nWhy (as recorded):")
        for w in a.why:
            W(f"“{w['text']}”  — {w['source']}", "  • ")
    if a.superseded:
        print("\nOverridden earlier — do not rely on:")
        for s in a.superseded:
            for by in s["superseded_by"]:
                W(f"{s['date']}  {s['title']}  →  overridden {by['date']} ({by['rule']}: {by['evidence']})", "  ✗ ")
    if a.chain:
        print("\nEvidence chain:")
        for c in a.chain:
            if "edge" in c:
                W(f"{c['date']}  └─ SUPERSEDES  [{c['rule']}] {c['evidence']}", "    ")
            else:
                pr = f"#{c['pr']} " if c.get("pr") else ""
                W(f"{c['date']}  [{c['status']}] {pr}{c['title']}  ({', '.join(c.get('people') or [])})", "  ")
    if a.ask:
        print("\nPerson to ask:")
        for p in a.ask[:3]:
            st = "active" if p["active"] else f"inactive since {p['last_active']}"
            print(f"  → {p['name']}  ({st})")
    if a.confidence_parts:
        print("\nConfidence is built from: " + ", ".join(f"{k}={v}" for k, v in a.confidence_parts.items()))
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kyun", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build the decision graph from a local git clone")
    b.add_argument("repo")
    b.add_argument("--name", help="owner/repo (used for links)")
    b.add_argument("--out", default="graph.json.gz")
    b.add_argument("--github-cache", help="directory of cached GitHub threads (see fetch-github)")

    a = sub.add_parser("ask", help="ask why")
    a.add_argument("graph")
    a.add_argument("question", nargs="+")
    a.add_argument("--json", action="store_true")
    a.add_argument("--lang", help="translate the answer with Sarvam (e.g. hi-IN, te-IN, ta-IN); needs SARVAM_API_KEY")

    s = sub.add_parser("serve", help="web UI")
    s.add_argument("graph")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")

    e = sub.add_parser("eval", help="score against a labelled question set")
    e.add_argument("graph")
    e.add_argument("questions")
    e.add_argument("--md")
    e.add_argument("--json-out")

    r = sub.add_parser("risk", help="knowledge at risk and stale release notes")
    r.add_argument("graph")

    st = sub.add_parser("stats", help="graph overview")
    st.add_argument("graph")

    p = sub.add_parser("push", help="write the graph to Neo4j / AuraDB")
    p.add_argument("graph")
    for x in (p,):
        x.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
        x.add_argument("--user", default=os.environ.get("NEO4J_USERNAME", "neo4j"))
        x.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
        x.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))

    pl = sub.add_parser("pull", help="read the graph back from Neo4j / AuraDB")
    pl.add_argument("--uri", default=os.environ.get("NEO4J_URI"))
    pl.add_argument("--user", default=os.environ.get("NEO4J_USERNAME", "neo4j"))
    pl.add_argument("--password", default=os.environ.get("NEO4J_PASSWORD"))
    pl.add_argument("--database", default=os.environ.get("NEO4J_DATABASE"))
    pl.add_argument("--out", default="graph.json.gz")

    c = sub.add_parser("export-csv", help="CSV + Cypher for LOAD CSV import")
    c.add_argument("graph")
    c.add_argument("out_dir")

    f = sub.add_parser("fetch-github", help="cache PR/issue discussions for numbers found in history")
    f.add_argument("graph")
    f.add_argument("--cache", default=".kyun/github")
    f.add_argument("--limit", type=int, default=500)

    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):      # Windows consoles default to cp1252
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.cmd == "build":
        from .graph.build import build_graph
        g = build_graph(args.repo, args.name, args.github_cache)
        g.save(args.out)
        st_ = g.stats()
        print(f"graph: {st_['total_nodes']} nodes, {st_['total_relationships']} relationships "
              f"in {g.meta['seconds']}s -> {args.out}")
    elif args.cmd == "ask":
        from .qa.engine import WhyEngine
        g = _load(args.graph)
        ans = WhyEngine(g).ask(" ".join(args.question))
        if args.lang:
            from .llm.sarvam import translate_answer
            translate_answer(ans, args.lang)
        if args.json:
            print(json.dumps(ans.to_dict(), indent=2, ensure_ascii=False))
        else:
            _print_answer(ans)
    elif args.cmd == "serve":
        from .web.app import create_app
        create_app(args.graph).run(host=args.host, port=args.port)
    elif args.cmd == "eval":
        from .evaluate import run_eval, to_markdown
        res = run_eval(_load(args.graph), json.load(open(args.questions, encoding="utf-8")))
        md = to_markdown(res)
        print(md)
        if args.md:
            open(args.md, "w", encoding="utf-8").write(md)
        if args.json_out:
            json.dump(res, open(args.json_out, "w", encoding="utf-8"), indent=2, ensure_ascii=False, default=str)
    elif args.cmd == "risk":
        from .insights import knowledge_at_risk, stale_release_notes
        g = _load(args.graph)
        print("Knowledge at risk (every decision-maker inactive 18+ months):")
        for row in knowledge_at_risk(g):
            print(f"  {row['symbol']:<55} {row['decisions']:>3} decisions  known by {', '.join(row['known_by'])}"
                  f"  (last seen {row['last_seen']})")
        print("\nRelease notes describing decisions that were later overridden:")
        for row in stale_release_notes(g, 10):
            print(f"  {row['version']:<8} {row['note'][:70]}…\n           overridden {row['on']}: {row['overridden_by'][:60]} [{row['rule']}]")
    elif args.cmd == "stats":
        from .insights import overview
        print(json.dumps(overview(_load(args.graph)), indent=2, ensure_ascii=False))
    elif args.cmd == "push":
        from .graph.neo4j_store import push
        if not (args.uri and args.password):
            sys.exit("set --uri/--password or NEO4J_URI/NEO4J_PASSWORD")
        print(json.dumps(push(_load(args.graph), args.uri, args.user, args.password, args.database), indent=2))
    elif args.cmd == "pull":
        from .graph.neo4j_store import pull
        g = pull(args.uri, args.user, args.password, args.database)
        g.save(args.out)
        print(f"pulled {g.stats()['total_nodes']} nodes -> {args.out}")
    elif args.cmd == "export-csv":
        from .graph.neo4j_store import export_csv
        files = export_csv(_load(args.graph), args.out_dir)
        print(f"wrote {len(files)} files to {args.out_dir} (load with load.cypher)")
    elif args.cmd == "fetch-github":
        from .ingest.github_api import GitHubClient
        g = _load(args.graph)
        owner, repo = g.meta["repo"].split("/", 1)
        cli = GitHubClient(owner, repo, args.cache)
        nums = sorted({g.node(p)["number"] for p in g.by_label("PullRequest")} |
                      {g.node(i)["number"] for i in g.by_label("Issue")}, reverse=True)[: args.limit]
        for i, n in enumerate(nums, 1):
            cli.thread(n)
            if i % 25 == 0:
                print(f"  {i}/{len(nums)}")
        print(f"cached {len(nums)} threads in {args.cache}; rebuild with --github-cache {args.cache}")


if __name__ == "__main__":
    main()
