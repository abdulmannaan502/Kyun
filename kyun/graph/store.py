"""Property-graph store.

`MemoryGraph` is an embedded store used for offline runs, tests and the demo.
It follows the same node-label / relationship-type model as the Neo4j schema
in cypher/schema.cypher, and `kyun.graph.neo4j_store` writes the identical
graph into Neo4j / AuraDB.
"""
from __future__ import annotations

import gzip
import json
from collections import defaultdict

LABELS = ("Commit", "PullRequest", "Issue", "Incident", "Release", "ReleaseNote",
          "Decision", "Person", "File", "Symbol", "ConfigKey", "Concept", "Discussion")

REL_TYPES = ("AUTHORED", "TOUCHES", "SETS", "DEFINED_IN", "PART_OF", "MADE_IN",
             "DOCUMENTED_IN", "IN_RELEASE", "ABOUT", "RESPONDS_TO", "REFERENCES",
             "SUPERSEDES", "DECIDED_BY", "DISCUSSED_IN", "RENAMED_TO")


class MemoryGraph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.label: dict[str, str] = {}
        self._out: dict[str, dict[str, list[tuple[str, dict]]]] = defaultdict(lambda: defaultdict(list))
        self._in: dict[str, dict[str, list[tuple[str, dict]]]] = defaultdict(lambda: defaultdict(list))
        self._edge_keys: set[tuple[str, str, str]] = set()
        self.meta: dict = {}

    # ---- writes -------------------------------------------------------
    def add_node(self, nid: str, label: str, **props) -> str:
        assert label in LABELS, label
        if nid in self.nodes:
            self.nodes[nid].update({k: v for k, v in props.items() if v is not None})
        else:
            self.nodes[nid] = {"id": nid, **props}
            self.label[nid] = label
        return nid

    def add_edge(self, src: str, rel: str, dst: str, **props) -> bool:
        assert rel in REL_TYPES, rel
        key = (src, rel, dst)
        if key in self._edge_keys:
            for d, p in self._out[src][rel]:
                if d == dst:
                    p.update(props)
            return False
        self._edge_keys.add(key)
        shared = dict(props)            # one object, seen from both ends
        self._out[src][rel].append((dst, shared))
        self._in[dst][rel].append((src, shared))
        return True

    # ---- reads --------------------------------------------------------
    def node(self, nid: str) -> dict:
        return self.nodes.get(nid, {})

    def lbl(self, nid: str) -> str:
        return self.label.get(nid, "")

    def out(self, nid: str, rel: str | None = None):
        rels = [rel] if rel else list(self._out[nid].keys())
        for r in rels:
            for dst, p in self._out[nid].get(r, []):
                yield dst, r, p

    def inc(self, nid: str, rel: str | None = None):
        rels = [rel] if rel else list(self._in[nid].keys())
        for r in rels:
            for src, p in self._in[nid].get(r, []):
                yield src, r, p

    def by_label(self, label: str):
        return [n for n, l in self.label.items() if l == label]

    def stats(self) -> dict:
        nodes = defaultdict(int)
        for l in self.label.values():
            nodes[l] += 1
        rels = defaultdict(int)
        for (_, r, _) in self._edge_keys:
            rels[r] += 1
        return {"nodes": dict(sorted(nodes.items())), "relationships": dict(sorted(rels.items())),
                "total_nodes": len(self.nodes), "total_relationships": len(self._edge_keys)}

    def edges(self):
        for src, rels in self._out.items():
            for r, lst in rels.items():
                for dst, p in lst:
                    yield src, r, dst, p

    # ---- persistence ---------------------------------------------------
    def save(self, path: str) -> None:
        data = {"meta": self.meta,
                "nodes": [{"label": self.label[n], **p} for n, p in self.nodes.items()],
                "edges": [[s, r, d, p] for s, r, d, p in self.edges()]}
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"), default=list)

    @classmethod
    def load(cls, path: str) -> "MemoryGraph":
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        g = cls()
        g.meta = data.get("meta", {})
        for n in data["nodes"]:
            label = n.pop("label")
            g.add_node(n.pop("id"), label, **n)
        for s, r, d, p in data["edges"]:
            g.add_edge(s, r, d, **p)
        return g
