"""Entity resolution: the hard part of the problem.

* People: one engineer, many identities (work email, personal email, GitHub
  noreply address, with and without a middle name). Merged with union-find on
  shared e-mail or shared normalised name.
* Files: a module that moved (requests/adapters.py -> src/requests/adapters.py)
  is still the same module. Rename chains from `git log -M` are followed to the
  newest path, which becomes the canonical id.
* Symbols: `path::name` keyed on the canonical path, so a function keeps its
  history across file moves; explicit renames ("Move X to Y") become aliases.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict


class UnionFind:
    def __init__(self):
        self.p: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def norm_name(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", n)


def name_variants(name: str) -> set[str]:
    """'Ian Stapleton Cordasco' also answers to 'Ian Cordasco'."""
    toks = [t for t in re.split(r"\s+", name.strip()) if t]
    if re.search(r"\b(and|&)\b|,", name):      # "A and B" pair commits: keep the literal only
        return {norm_name(name)}
    out = {norm_name(name)}
    if len(toks) >= 3:
        out.add(norm_name(toks[0] + toks[-1]))
    return {v for v in out if len(v) >= 5}


def norm_email(email: str) -> str:
    e = email.strip().lower()
    m = re.match(r"(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", e)
    return f"gh:{m.group(1)}" if m else e


class PeopleResolver:
    def __init__(self):
        self.uf = UnionFind()
        self.names: dict[str, Counter] = defaultdict(Counter)
        self.emails: dict[str, set] = defaultdict(set)
        self.handles: dict[str, set] = defaultdict(set)
        self._raw: list[tuple[str, str]] = []

    def add(self, name: str, email: str) -> None:
        e = "e:" + norm_email(email)
        self.uf.find(e)
        for v in name_variants(name):
            self.uf.union(e, "n:" + v)
        self._raw.append((name, email))

    def finalize(self) -> None:
        for name, email in self._raw:
            root = self.uf.find("e:" + norm_email(email))
            self.names[root][name] += 1
            self.emails[root].add(email.lower())
            ne = norm_email(email)
            if ne.startswith("gh:"):
                self.handles[root].add(ne[3:])

    def person_id(self, name: str, email: str) -> str:
        return "person:" + self.uf.find("e:" + norm_email(email)).split(":", 1)[1]

    def display(self, pid: str) -> tuple[str, list[str], list[str], list[str]]:
        root = "e:" + pid.split(":", 1)[1]
        names = self.names.get(root) or Counter()
        main = names.most_common(1)[0][0] if names else pid
        return main, sorted(names), sorted(self.emails.get(root, [])), sorted(self.handles.get(root, []))

    def roots(self):
        return list(self.names.keys())


def canonical_paths(rename_pairs: list[tuple[str, str]]) -> dict[str, str]:
    """old -> newest path, following chains in commit order."""
    nxt: dict[str, str] = {}
    for old, new in rename_pairs:
        if old != new:
            nxt[old] = new
    cache: dict[str, str] = {}

    def resolve(p: str) -> str:
        seen = set()
        while p in nxt and p not in seen:
            seen.add(p)
            p = nxt[p]
        return p

    for p in list(nxt):
        cache[p] = resolve(p)
    return cache


def module_label(path: str) -> str:
    """src/requests/adapters.py -> requests.adapters"""
    p = re.sub(r"^(src|lib)/", "", path)
    p = re.sub(r"\.py$", "", p).replace("/__init__", "")
    return p.replace("/", ".")
