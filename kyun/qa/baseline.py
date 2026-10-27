"""The thing Kyun is compared against: flat retrieval over the same text.

Every commit message and every release note is a chunk; BM25 ranks them;
the top chunk is "the answer". This is what a search box or a vector-RAG
setup over the history gives you. Same data, no graph.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from ..extract import text as tx
from .engine import stem


class FlatSearch:
    def __init__(self, g, k1: float = 1.4, b: float = 0.75):
        self.g = g
        self.docs: list[tuple[str, str, str]] = []   # (id, date, text)
        for cid in g.by_label("Commit"):
            n = g.node(cid)
            if not n.get("is_merge"):
                self.docs.append((cid, n["date"], n["message"]))
        for rn in g.by_label("ReleaseNote"):
            n = g.node(rn)
            self.docs.append((rn, n["date"], f"{n['version']}: {n['text']}"))
        self.tf = [Counter(self._tok(t)) for _, _, t in self.docs]
        self.len = [sum(c.values()) for c in self.tf]
        self.avg = sum(self.len) / max(1, len(self.len))
        df = Counter()
        for c in self.tf:
            df.update(c.keys())
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.k1, self.b = k1, b

    @staticmethod
    def _tok(text: str) -> list[str]:
        toks = [stem(w) for w in tx.words(text)]
        toks += ["".join(tx.split_ident(c)) for c in tx.concepts(text)]
        return toks

    def search(self, question: str, k: int = 3) -> list[dict]:
        q = [t for t in self._tok(question)]
        scores = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        out = []
        for s, i in scores[:k]:
            did, dt, text = self.docs[i]
            out.append({"id": did, "date": dt, "score": round(s, 2), "text": re.sub(r"\s+", " ", text)[:300]})
        return out
