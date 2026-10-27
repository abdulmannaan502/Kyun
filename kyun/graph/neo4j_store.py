"""Neo4j / AuraDB as the system of record.

`push(graph, uri, user, password)` writes the decision graph into Neo4j with
idempotent MERGEs in batches. `pull(uri, user, password)` reads it back into a
MemoryGraph so the same WhyEngine answers questions on top of AuraDB.

Neo4j properties must be primitives or lists of primitives, so nested values
(rationale entries, edge evidence) are stored as JSON strings with a `_json`
suffix and decoded on pull.

Requires `pip install neo4j` and an AuraDB Free instance (or any Neo4j 5.x).
"""
from __future__ import annotations

import json
from collections import defaultdict

from .store import LABELS, MemoryGraph

BATCH = 2000


def _flat(props: dict) -> dict:
    out = {}
    for k, v in props.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, (list, tuple, set)):
            v = list(v)
            if all(isinstance(x, (str, int, float, bool)) for x in v):
                out[k] = v
            else:
                out[k + "_json"] = json.dumps(v, ensure_ascii=False)
        else:
            out[k + "_json"] = json.dumps(v, ensure_ascii=False, default=str)
    return out


def _unflat(props: dict) -> dict:
    out = {}
    for k, v in props.items():
        if k.endswith("_json") and isinstance(v, str):
            out[k[:-5]] = json.loads(v)
        else:
            out[k] = v
    return out


def schema_statements() -> list[str]:
    stmts = [f"CREATE CONSTRAINT {l.lower()}_id IF NOT EXISTS FOR (n:{l}) REQUIRE n.id IS UNIQUE" for l in LABELS]
    stmts += [
        "CREATE INDEX decision_date IF NOT EXISTS FOR (d:Decision) ON (d.date)",
        "CREATE INDEX symbol_name IF NOT EXISTS FOR (s:Symbol) ON (s.name)",
        "CREATE INDEX config_name IF NOT EXISTS FOR (c:ConfigKey) ON (c.name)",
        "CREATE INDEX concept_key IF NOT EXISTS FOR (c:Concept) ON (c.key)",
        "CREATE FULLTEXT INDEX decision_text IF NOT EXISTS FOR (d:Decision) ON EACH [d.title, d.text]",
    ]
    return stmts


def write_batches(g: MemoryGraph):
    """Yield (cypher, params) pairs; separated from the driver so it can be tested offline."""
    by_label = defaultdict(list)
    for nid, props in g.nodes.items():
        by_label[g.lbl(nid)].append(_flat(props))
    for label, rows in by_label.items():
        for i in range(0, len(rows), BATCH):
            yield (f"UNWIND $rows AS row MERGE (n:{label} {{id: row.id}}) SET n += row",
                   {"rows": rows[i:i + BATCH]})
    by_rel = defaultdict(list)
    for s, r, d, p in g.edges():
        by_rel[(g.lbl(s), r, g.lbl(d))].append({"s": s, "d": d, "p": _flat(p)})
    for (ls, r, ld), rows in by_rel.items():
        for i in range(0, len(rows), BATCH):
            yield (f"UNWIND $rows AS row MATCH (a:{ls} {{id: row.s}}) MATCH (b:{ld} {{id: row.d}}) "
                   f"MERGE (a)-[e:{r}]->(b) SET e += row.p", {"rows": rows[i:i + BATCH]})
    yield ("MERGE (m:KyunMeta {id: 'meta'}) SET m += $meta", {"meta": _flat(g.meta)})


def push(g: MemoryGraph, uri: str, user: str, password: str, database: str | None = None, log=print) -> dict:
    from neo4j import GraphDatabase   # imported lazily: optional dependency
    n = 0
    with GraphDatabase.driver(uri, auth=(user, password)) as drv:
        drv.verify_connectivity()
        with drv.session(database=database) as s:
            for stmt in schema_statements():
                s.run(stmt)
            for cypher, params in write_batches(g):
                s.run(cypher, params).consume()
                n += 1
                if n % 10 == 0:
                    log(f"  {n} batches written")
            counts = s.run("MATCH (n) RETURN count(n) AS nodes").single()["nodes"]
            rels = s.run("MATCH ()-[r]->() RETURN count(r) AS rels").single()["rels"]
    return {"batches": n, "nodes": counts, "relationships": rels}


def pull(uri: str, user: str, password: str, database: str | None = None) -> MemoryGraph:
    from neo4j import GraphDatabase
    g = MemoryGraph()
    with GraphDatabase.driver(uri, auth=(user, password)) as drv:
        with drv.session(database=database) as s:
            for rec in s.run("MATCH (n) WHERE NOT n:KyunMeta RETURN labels(n)[0] AS l, properties(n) AS p"):
                p = _unflat(dict(rec["p"]))
                g.add_node(p.pop("id"), rec["l"], **p)
            for rec in s.run("MATCH (a)-[r]->(b) WHERE NOT a:KyunMeta "
                             "RETURN a.id AS s, type(r) AS t, b.id AS d, properties(r) AS p"):
                g.add_edge(rec["s"], rec["t"], rec["d"], **_unflat(dict(rec["p"])))
            m = s.run("MATCH (m:KyunMeta {id:'meta'}) RETURN properties(m) AS p").single()
            g.meta = _unflat(dict(m["p"])) if m else {}
    return g


def export_csv(g: MemoryGraph, out_dir: str) -> list[str]:
    """CSV files for LOAD CSV / neo4j-admin import, plus the Cypher to load them."""
    import csv
    import os
    os.makedirs(out_dir, exist_ok=True)
    files = []
    by_label = defaultdict(list)
    for nid, props in g.nodes.items():
        by_label[g.lbl(nid)].append(_flat(props))
    load = []
    for label, rows in by_label.items():
        cols = sorted({k for r in rows for k in r})
        path = os.path.join(out_dir, f"nodes_{label}.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for r in rows:
                w.writerow([json.dumps(r[c], ensure_ascii=False) if isinstance(r.get(c), list) else r.get(c, "") for c in cols])
        files.append(path)
        sets = ", ".join(f"n.{c} = row.{c}" for c in cols if c != "id")
        load.append(f"LOAD CSV WITH HEADERS FROM 'file:///nodes_{label}.csv' AS row\n"
                    f"CALL {{ WITH row MERGE (n:{label} {{id: row.id}}) SET {sets} }} IN TRANSACTIONS OF 5000 ROWS;")
    by_rel = defaultdict(list)
    for s, r, d, p in g.edges():
        by_rel[r].append((s, d, g.lbl(s), g.lbl(d), _flat(p)))
    for r, rows in by_rel.items():
        cols = sorted({k for *_, p in rows for k in p})
        path = os.path.join(out_dir, f"rels_{r}.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["start", "end", "start_label", "end_label"] + cols)
            for s, d, ls, ld, p in rows:
                w.writerow([s, d, ls, ld] + [p.get(c, "") for c in cols])
        files.append(path)
        sets = (" SET " + ", ".join(f"e.{c} = row.{c}" for c in cols)) if cols else ""
        load.append(f"LOAD CSV WITH HEADERS FROM 'file:///rels_{r}.csv' AS row\n"
                    f"CALL {{ WITH row MATCH (a {{id: row.start}}) MATCH (b {{id: row.end}}) "
                    f"MERGE (a)-[e:{r}]->(b){sets} }} IN TRANSACTIONS OF 5000 ROWS;")
    path = os.path.join(out_dir, "load.cypher")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("// Run after copying the CSVs into Neo4j's import directory\n")
        fh.write(";\n".join(schema_statements()) + ";\n\n" + "\n\n".join(load) + "\n")
    files.append(path)
    return files
