"""Answer "why is it like this?" from the decision graph, with evidence.

Pipeline for one question
  1. link     words in the question -> code entities and concepts in the graph
  2. expand   entities -> the decisions ABOUT them (structure, not text similarity)
  3. thread   decisions -> chains connected by SUPERSEDES; the live decision is
              the one nothing later overrode
  4. explain  rationale spans + evidence chain + person to ask + confidence
  5. refuse   when the graph does not contain a reason, say so
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from ..extract import text as tx

GENERIC = set("""code file files function functions module class default value values change changes
changed thing things way work works used uses project library package support supports version versions
behaviour behavior call calls method methods new old still always never anymore any longer instead like
api object objects param params parameter parameters option options feature features set sets setting""".split())


def stem(w: str) -> str:
    """Tiny, consistent stemmer: cookie/cookies, retry/retries, cache/caching/cached."""
    w = w.lower()
    if len(w) > 4 and w.endswith("ies"):
        w = w[:-3] + "i"
    elif len(w) > 4 and w.endswith(("ches", "shes", "xes", "sses")):
        w = w[:-2]
    elif len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suf in ("ing", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            w = w[: -len(suf)]
            if len(w) > 3 and w[-1] == w[-2] and w[-1] in "bdgmnprt":
                w = w[:-1]          # dropped -> drop, mapped -> map
            break
    if len(w) > 3 and w.endswith("y"):
        w = w[:-1] + "i"
    if len(w) > 4 and w.endswith("e"):
        w = w[:-1]
    return w


@dataclass
class Answer:
    question: str
    status: str                      # answered | what_not_why | not_in_graph
    headline: str
    why: list[dict] = field(default_factory=list)
    current: dict | None = None
    superseded: list[dict] = field(default_factory=list)
    chain: list[dict] = field(default_factory=list)
    ask: list[dict] = field(default_factory=list)
    related: list[dict] = field(default_factory=list)
    linked: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    confidence_label: str = "none"
    confidence_parts: dict = field(default_factory=dict)
    subgraph: dict = field(default_factory=dict)
    cypher: str = ""

    def to_dict(self) -> dict:
        return self.__dict__


NUM_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "twenty": 20, "hundred": 100}


def premise_number(q: str):
    m = re.search(r"\b(\d+)\b", q)
    if m:
        return int(m.group(1))
    for w in re.findall(r"[a-z]+", q.lower()):
        if w in NUM_WORDS:
            return NUM_WORDS[w]
    return None


WHO_RE = re.compile(r"^\s*who\b|\bwho (?:should|can|do|to|would|knows?)\b|\bwhom\b|\bowner of\b|\bwho owns\b", re.I)


class WhyEngine:
    def __init__(self, g):
        self.g = g
        self.repo = g.meta.get("repo", "")
        self.repo_words = set(tx.split_ident(self.repo.split("/")[-1])) | set(self.repo.lower().replace("/", " ").split())
        self.repo_words |= {stem(w) for w in self.repo_words}
        last = g.meta.get("last_commit") or date.today().isoformat()
        self.today = date.fromisoformat(last)
        self._build_index()

    # ------------------------------------------------------------------ index
    def _build_index(self):
        g = self.g
        self.key_index: dict[str, list[tuple[str, float]]] = defaultdict(list)
        deg = {}
        for nid in g.by_label("Symbol") + g.by_label("ConfigKey"):
            n = g.node(nid)
            if n.get("is_test"):
                continue
            full = "".join(tx.split_ident(n["name"]))
            for k in tx.ident_keys(n["name"]) | {full}:
                self.key_index[k].append((nid, 1.0 if k == full else 0.5))
            if self.g.lbl(nid) == "ConfigKey":
                for part in tx.split_ident(n["name"]):
                    if len(part) >= 5 and part not in GENERIC:
                        self.key_index["~" + stem(part)].append((nid, 0.4))
        for nid in g.by_label("File"):
            n = g.node(nid)
            if n.get("is_test") or not n["path"].endswith(".py"):
                continue
            base = re.sub(r"\.py$", "", n["path"].split("/")[-1])
            if base != "__init__":
                self.key_index["".join(tx.split_ident(base))].append((nid, 0.8))
                self.key_index["~" + stem(base)].append((nid, 0.6))
        for nid in g.by_label("Concept"):
            n = g.node(nid)
            self.key_index[n["key"]].append((nid, 0.9))
            parts = tx.split_ident(n["forms"][0]) if n.get("forms") else []
            for i in range(len(parts) - 1):
                k = parts[i] + parts[i + 1]
                if k != n["key"] and len(k) >= 6 and not (parts[i].isdigit() or parts[i + 1].isdigit()):
                    self.key_index[k].append((nid, 0.45))
        for nid in g.by_label("Incident"):
            self.key_index[g.node(nid)["ref"].lower().replace("-", "")].append((nid, 1.0))
        # how many decisions each entity is about (specificity)
        for k, lst in self.key_index.items():
            for nid, _ in lst:
                if nid not in deg:
                    deg[nid] = sum(1 for _ in self.g.inc(nid, "ABOUT")) + sum(1 for _ in self.g.inc(nid, "RESPONDS_TO"))
        self.deg = deg
        # text index over decisions: title, rationale and release notes
        self.doc_terms: dict[str, set[str]] = {}
        df = defaultdict(int)
        for did in g.by_label("Decision"):
            d = g.node(did)
            blob = d["title"] + " " + " ".join(r["text"] for r in d["rationale"])
            terms = {stem(w) for w in tx.words(blob)}
            for k in tx.concepts(blob):
                terms.add("".join(tx.split_ident(k)))
            terms |= tx.number_terms(blob)
            self.doc_terms[did] = terms
            for t in terms:
                df[t] += 1
        n = max(1, len(self.doc_terms))
        self.idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}
        self.title_terms = {did: {stem(w) for w in tx.words(self.g.node(did)["title"])}
                            for did in self.doc_terms}

    # ------------------------------------------------------------------ 1. link
    def link(self, question: str) -> tuple[list[dict], list[str]]:
        q = question.strip()
        raw_words = re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", q)
        keys: dict[str, str] = {}
        factor: dict[str, float] = {}
        for tok in tx.concepts(q):
            full = "".join(tx.split_ident(tok))
            keys[full], factor[full] = tok, 1.25          # typed an exact identifier
            for k in tx.ident_keys(tok):
                if k not in keys:
                    keys[k], factor[k] = tok, 0.6
        low = [w.lower().strip(".") for w in raw_words]
        for i, w in enumerate(low):
            if w not in tx.STOP and w not in GENERIC and w not in self.repo_words and len(w) >= 4:
                keys.setdefault("".join(tx.split_ident(w)), raw_words[i])
                keys.setdefault("~" + stem(w), raw_words[i])
            for j in (2, 3):
                if i + j <= len(low):
                    seg = low[i:i + j]
                    if any(s in tx.STOP for s in seg):
                        continue
                    keys.setdefault("".join("".join(tx.split_ident(s)) for s in seg), " ".join(raw_words[i:i + j]))
        for cve in tx.CVE_RE.findall(q.upper()):
            keys[cve.lower().replace("-", "")] = cve
        seeds: dict[str, dict] = {}
        for k, surface in keys.items():
            for nid, w in self.key_index.get(k, []):
                spec = 1.0 / (1.0 + math.log10(1 + self.deg.get(nid, 0)))
                w = min(1.0, w * factor.get(k, 1.0))
                score = w * spec
                if nid not in seeds or seeds[nid]["weight"] < score:
                    seeds[nid] = {"id": nid, "label": self.g.lbl(nid), "name": self._name(nid),
                                  "matched": surface, "weight": round(score, 3), "match": round(w, 2)}
        terms = [stem(w) for w in tx.words(q) if w not in GENERIC and stem(w) not in self.repo_words]
        terms += [t for t in tx.number_terms(q) if t not in terms]
        terms += [k for k in keys if k in self.idf and k not in terms]
        return sorted(seeds.values(), key=lambda s: -s["weight"])[:12], terms

    def _name(self, nid: str) -> str:
        n, l = self.g.node(nid), self.g.lbl(nid)
        if l in ("Symbol", "ConfigKey"):
            return f"{n['module']}.{n['name']}"
        if l == "File":
            return n["path"]
        if l == "Concept":
            return (n.get("forms") or [n["key"]])[0]
        if l == "Incident":
            return n["ref"]
        return nid

    # ------------------------------------------------------------------ 2. expand
    def candidates(self, seeds, terms) -> dict[str, dict]:
        g = self.g
        cand: dict[str, dict] = defaultdict(lambda: {"struct": 0.0, "text": 0.0, "via": []})
        for s in seeds:
            rel = "RESPONDS_TO" if s["label"] == "Incident" else "ABOUT"
            for did, _, p in g.inc(s["id"], rel):
                if g.lbl(did) != "Decision":
                    continue
                w = s["weight"] * float(p.get("weight", 1.0))
                c = cand[did]
                c["struct"] += w
                c["via"].append({"entity": s["id"], "name": s["name"], "edge": rel,
                                 "change": p.get("change") or ("title" if p.get("in_title") else "mention"),
                                 "w": round(w, 3)})
            if s["label"] == "Concept":       # concept -> symbol it names -> decisions about that code
                for sid, _, _ in g.out(s["id"], "REFERENCES"):
                    for did, _, p in g.inc(sid, "ABOUT"):
                        if g.lbl(did) == "Decision":
                            w = 0.5 * s["weight"] * float(p.get("weight", 1.0))
                            cand[did]["struct"] += w
                            cand[did]["via"].append({"entity": sid, "name": self._name(sid), "edge": "ABOUT",
                                                     "change": p.get("change") or "edit", "w": round(w, 3)})
        total_idf = sum(self.idf.get(t, 0) for t in set(terms)) or 1.0
        for did in list(cand.keys()):
            hit = set(terms) & self.doc_terms.get(did, set())
            tit = set(terms) & self.title_terms.get(did, set())
            cand[did]["text"] = (sum(self.idf.get(t, 0) for t in hit) + 0.5 * sum(self.idf.get(t, 0) for t in tit)) / total_idf
        # decisions that state most of the question in words also come in,
        # but without structural support they are discounted
        for did, ts in self.doc_terms.items():
            if did in cand:
                continue
            hit = set(terms) & ts
            if len(hit) >= 2:
                cov = sum(self.idf.get(t, 0) for t in hit) / total_idf
                if cov >= 0.5:
                    cand[did]["text"] = (0.6 if not seeds else 0.8) * cov
        q_versions = {t for t in terms if re.fullmatch(r"[a-z]+\d+", t)}
        for did, c in cand.items():
            d = g.node(did)
            q = 1.0
            if q_versions:
                dts = self.doc_terms.get(did, set())
                for qv in q_versions:
                    word = re.match(r"[a-z]+", qv).group(0)
                    others = {t for t in dts if re.fullmatch(word + r"\d+", t)}
                    if others and qv not in others:
                        q *= 0.35       # about a different version of the same thing
            q *= 1.3 if d["rationale"] else 0.75
            q *= 1.15 if any(True for _ in g.out(did, "DOCUMENTED_IN")) else 1.0
            q *= 0.25 if d.get("low_signal") else 1.0
            q *= 1.1 if d["kind"] in ("revert", "security", "deprecate", "remove", "dependency") else 1.0
            c["quality"] = q
            c["score"] = (min(c["struct"], 2.5) + 1.6 * c["text"]) * q
        return cand

    def _version_conflict(self, did: str, terms) -> bool:
        """The question asks about python2; this decision is about python3 only."""
        dts = self.doc_terms.get(did, set())
        for qv in (t for t in terms if re.fullmatch(r"[a-z]+\d+", t)):
            word = re.match(r"[a-z]+", qv).group(0)
            others = {t for t in dts if re.fullmatch(word + r"\d+", t)}
            if others and qv not in others:
                return True
        return False

    # ------------------------------------------------------------------ 3. thread
    def threads(self, cand: dict[str, dict], terms: list[str] | None = None) -> list[dict]:
        g = self.g
        tset = set(terms or [])
        tot = sum(self.idf.get(t, 0) for t in tset) or 1.0
        top = sorted(cand, key=lambda d: -cand[d]["score"])[:25]
        seen, out = set(), []
        for did in top:
            if did in seen:
                continue
            comp, stack = set(), [did]
            while stack and len(comp) < 30:
                x = stack.pop()
                if x in comp:
                    continue
                comp.add(x)
                stack += [d for d, _, _ in g.out(x, "SUPERSEDES")] + [s for s, _, _ in g.inc(x, "SUPERSEDES")]
            seen |= comp
            scores = sorted((cand[d]["score"] for d in comp if d in cand), reverse=True)
            live = [d for d in comp if not any(s in comp for s, _, _ in g.inc(d, "SUPERSEDES"))]
            live.sort(key=lambda d: g.node(d)["date"], reverse=True)
            covered = set()
            for d in comp:
                covered |= tset & self.doc_terms.get(d, set())
            coverage = sum(self.idf.get(t, 0) for t in covered) / tot if tset else 1.0
            head = live[0] if live else did
            penalty = 0.35 if self._version_conflict(head, terms or []) else 1.0
            out.append({"members": comp, "live": head, "coverage": round(coverage, 2),
                        "score": (scores[0] + 0.25 * sum(scores[1:4])) * (0.45 + 0.55 * coverage) * penalty,
                        "entry": did})
        out.sort(key=lambda t: -t["score"])
        return out

    # ------------------------------------------------------------------ helpers
    def _dec_card(self, did: str) -> dict:
        g, d = self.g, self.g.node(did)
        made = [x for x, _, _ in g.out(did, "MADE_IN")]
        people = []
        for pid, _, _ in g.out(did, "DECIDED_BY"):
            p = g.node(pid)
            if not p.get("is_bot"):
                people.append({"id": pid, "name": p["name"], "last_active": p["last_active"]})
        notes = [{"id": rn, "version": g.node(rn)["version"], "date": g.node(rn)["date"],
                  "text": g.node(rn)["text"][:400], "how": p.get("how")} for rn, _, p in g.out(did, "DOCUMENTED_IN")]
        refs = []
        for x, r, _ in list(g.out(did, "RESPONDS_TO")) + list(g.out(did, "REFERENCES")):
            n = g.node(x)
            refs.append({"id": x, "label": g.lbl(x), "ref": n.get("ref") or f"#{n.get('number')}"})
        return {"id": did, "title": d["title"], "date": d["date"], "kind": d["kind"], "pr": d.get("pr"),
                "source": d["source"], "shas": [s[:8] for s in d.get("shas", [])[:6]],
                "made_in": made, "people": people[:4], "notes": notes[:3], "refs": refs[:5],
                "rationale": d["rationale"][:5], "url": self._url(d)}

    def _url(self, d) -> str | None:
        if "/" not in self.repo:
            return None
        if d.get("pr"):
            return f"https://github.com/{self.repo}/pull/{d['pr']}"
        if d.get("shas"):
            return f"https://github.com/{self.repo}/commit/{d['shas'][-1]}"
        return None

    def _staleness(self, did: str, seeds) -> int:
        """How many later decisions touched the same code since this one."""
        g, d = self.g, self.g.node(did)
        code = {x for x, _, p in g.out(did, "ABOUT") if g.lbl(x) in ("Symbol", "ConfigKey")}
        later = set()
        for x in code:
            for other, _, _ in g.inc(x, "ABOUT"):
                if other != did and g.node(other).get("date", "") > d["date"] and not g.node(other).get("low_signal"):
                    later.add(other)
        return len(later)

    def people_to_ask(self, entity_ids: list[str], decision_ids: list[str]) -> list[dict]:
        g = self.g
        score = defaultdict(float)
        why = defaultdict(list)
        for eid in entity_ids:
            if g.lbl(eid) not in ("Symbol", "ConfigKey", "File"):
                continue
            for did, _, _ in g.inc(eid, "ABOUT"):
                d = g.node(did)
                if g.lbl(did) != "Decision" or d.get("low_signal"):
                    continue
                age = (self.today - date.fromisoformat(d["date"])).days / 365.0 if d.get("date") else 10
                for pid, _, _ in g.out(did, "DECIDED_BY"):
                    score[pid] += math.exp(-age / 2.0)
        for did in decision_ids:
            for pid, _, _ in g.out(did, "DECIDED_BY"):
                score[pid] += 1.5
                why[pid].append(g.node(did)["title"][:70])
        out = []
        for pid, s in score.items():
            p = g.node(pid)
            if p.get("is_bot"):
                continue
            gap = (self.today - date.fromisoformat(p["last_active"])).days
            active = gap <= 540
            out.append({"id": pid, "name": p["name"], "aliases": p.get("names", []), "handles": p.get("handles", []),
                        "last_active": p["last_active"], "active": active,
                        "score": round(s * (1.0 if active else 0.35), 3), "decided": why.get(pid, [])[:2]})
        out.sort(key=lambda x: -x["score"])
        return out[:3]

    # ------------------------------------------------------------------ 4. answer
    def ask(self, question: str) -> Answer:
        g = self.g
        seeds, terms = self.link(question)
        cand = self.candidates(seeds, terms)
        threads = self.threads(cand, terms)
        link_strength = max([s["match"] for s in seeds], default=0.0)
        ans = Answer(question=question, status="not_in_graph", headline="", linked=seeds[:6])
        ans.cypher = CYPHER_WHY
        unknown = self.unknown_terms(question)

        if WHO_RE.search(question) and seeds:
            return self._answer_who(ans, seeds)

        if unknown and not (threads and any(self._mentions(t, unknown) for t in threads[:1])):
            entities = [s["id"] for s in seeds if s["label"] in ("Symbol", "ConfigKey", "File")]
            ans.ask = self.people_to_ask(entities, [])
            ans.headline = (f"Nothing in this repository's history mentions {', '.join(repr(u) for u in unknown)}. "
                            f"I can't tell you why, and I won't guess.")
            ans.related = [self._dec_card(t["live"]) for t in threads[:2]]
            ans.subgraph = self._subgraph(seeds[:3], [], ans.ask)
            return ans

        code_seed = next((s for s in seeds if s["label"] in ("Symbol", "ConfigKey") and s["match"] >= 1.0), None)
        if code_seed is None and seeds and seeds[0]["label"] == "ConfigKey":
            code_seed = seeds[0]
        if code_seed and (not threads or threads[0]["score"] < 0.9 or threads[0]["coverage"] < 0.3):
            return self._answer_code_history(ans, code_seed, seeds)

        if not threads or threads[0]["score"] < 0.9 or threads[0]["coverage"] < 0.25:
            entities = [s["id"] for s in seeds if s["label"] in ("Symbol", "ConfigKey", "File")]
            ans.ask = self.people_to_ask(entities, [])
            ans.headline = ("I could not find a recorded decision that answers this in the repository's history. "
                            "I won't guess.")
            if seeds:
                ans.headline += f" The closest things I know about are {', '.join(s['name'] for s in seeds[:3])}."
            ans.confidence, ans.confidence_label = 0.0, "none"
            ans.related = [self._dec_card(t["live"]) for t in threads[:3]]
            ans.subgraph = self._subgraph(seeds[:4], [], ans.ask)
            return ans

        best = threads[0]
        live = best["live"]
        members = sorted(best["members"], key=lambda d: g.node(d)["date"])
        sup = [d for d in members if d != live and any(True for _ in g.inc(d, "SUPERSEDES"))]
        cur = self._dec_card(live)
        ans.current = cur
        for d in sup:
            card = self._dec_card(d)
            card["superseded_by"] = [{"id": s, "title": g.node(s)["title"], "date": g.node(s)["date"],
                                      "rule": p["rule"], "evidence": p["evidence"]}
                                     for s, _, p in g.inc(d, "SUPERSEDES")]
            ans.superseded.append(card)

        # rationale: prefer release notes / PR text over restated commit subjects
        rank = {"release": 0, "PR": 1, "comment": 2, "commit": 3}
        why = sorted(cur["rationale"], key=lambda r: rank.get(r["source"].split()[0], 4))
        ans.why = why[:3]

        # evidence chain, oldest first
        for d in members:
            node = g.node(d)
            step = {"date": node["date"], "decision": d, "title": node["title"], "kind": node["kind"],
                    "pr": node.get("pr"), "people": [p["name"] for p in self._dec_card(d)["people"]][:2],
                    "status": "live" if d == live else ("superseded" if d in sup else "related")}
            ans.chain.append(step)
            for s, _, p in g.inc(d, "SUPERSEDES"):
                if s in best["members"]:
                    ans.chain.append({"date": g.node(s)["date"], "edge": "SUPERSEDES", "from": s, "to": d,
                                      "rule": p["rule"], "evidence": p["evidence"]})
        ans.chain.sort(key=lambda x: (x["date"], 0 if "decision" in x else 1))

        entities = [s["id"] for s in seeds if s["label"] in ("Symbol", "ConfigKey", "File")]
        entities += [x for x, _, _ in g.out(live, "ABOUT") if g.lbl(x) in ("Symbol", "ConfigKey")][:6]
        ans.ask = self.people_to_ask(entities, [live] + sup)
        ans.related = [self._dec_card(t["live"]) for t in threads[1:4]]

        # ---- confidence: every part is shown to the user
        runner = threads[1]["score"] if len(threads) > 1 else 0.0
        margin = max(0.0, min(1.0, (best["score"] - runner) / best["score"]))
        sources = {r["source"].split()[0] for r in cur["rationale"]}
        has_why = bool(why)
        stale = self._staleness(live, seeds)
        corroboration = min(1.0, (len(sources) + (1 if cur["notes"] else 0) + (1 if sup else 0)) / 3)
        link_part = max(link_strength, 0.35 if not seeds else link_strength)
        parts = {"entity link": round(link_part, 2), "reason recorded": 1.0 if has_why else 0.0,
                 "corroboration": round(corroboration, 2), "margin over next answer": round(margin, 2),
                 "later changes to same code": stale}
        conf = 0.30 * link_part + 0.30 * (1.0 if has_why else 0.0) + 0.25 * corroboration + 0.15 * margin
        if stale >= 8:
            conf *= 0.85
        if not seeds:
            conf = min(conf, 0.45)
        ans.confidence = round(conf, 2)
        ans.confidence_label = "high" if conf >= 0.7 else "medium" if conf >= 0.5 else "low"
        ans.confidence_parts = parts

        when = cur["date"]
        who = ", ".join(p["name"] for p in cur["people"][:2]) or "an unknown author"
        title = cur["title"]
        if cur["source"] == "release notes":
            sents = tx.sentences(g.node(live).get("text") or title)
            title = (sents[0] if sents else title).rstrip(".")
            ref = f"the {cur['notes'][0]['version']} release notes" if cur["notes"] else "the release notes"
            ans.why = [w for w in ans.why if w["text"].rstrip(".") != title] or ans.why
            why = ans.why
        else:
            ref = f"#{cur['pr']}" if cur.get("pr") else (cur["shas"][-1] if cur["shas"] else "release notes")
        if has_why:
            ans.status = "answered"
            ans.headline = f"{title} — decided {when} in {ref} by {who}."
            if sup:
                old = ans.superseded[-1]
                ans.headline += (f" This overrides an earlier decision (\"{old['title']}\", {old['date']}), "
                                 f"which you may still find quoted in older docs.")
        else:
            ans.status = "what_not_why"
            ans.confidence = min(ans.confidence, 0.45)
            ans.confidence_label = "low"
            ans.headline = (f"The history shows what happened — \"{title}\" on {when} in {ref} by {who} — "
                            f"but nobody wrote down why. Ask {ans.ask[0]['name'] if ans.ask else 'the maintainers'}.")
        ans.subgraph = self._subgraph(seeds[:4], members, ans.ask)
        return ans

    # ------------------------------------------------------------------ special cases
    TECH_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./+-]*[A-Za-z0-9]")

    def unknown_terms(self, question: str) -> list[str]:
        """Technical-looking words that match nothing in the graph (e.g. 'HTTP/2')."""
        out = []
        for tok in self.TECH_RE.findall(question):
            technical = (re.search(r"\d", tok) or "_" in tok or "/" in tok or "." in tok.strip(".")
                         or re.search(r"[a-z][A-Z]", tok) or (tok.isupper() and len(tok) >= 3))
            if not technical:
                continue
            key = re.sub(r"[^a-z0-9]", "", tok.lower())
            parts = "".join(tx.split_ident(tok))
            if key in self.key_index or parts in self.key_index or key in self.idf or parts in self.idf \
                    or stem(key) in self.idf:
                continue
            out.append(tok)
        return out

    def _mentions(self, thread, terms) -> bool:
        keys = {re.sub(r"[^a-z0-9]", "", t.lower()) for t in terms}
        for d in thread["members"]:
            blob = re.sub(r"[^a-z0-9]", "", (self.g.node(d).get("text") or self.g.node(d)["title"]).lower())
            if any(k in blob for k in keys):
                return True
        return False

    def _answer_who(self, ans: "Answer", seeds) -> "Answer":
        entities = [s["id"] for s in seeds if s["label"] in ("Symbol", "ConfigKey", "File")]
        if not entities:   # a concept: use the decisions about it
            decs = [d for s in seeds for d, _, _ in self.g.inc(s["id"], "ABOUT")][:30]
            ans.ask = self.people_to_ask([], decs)
        else:
            ans.ask = self.people_to_ask(entities, [])
        if not ans.ask:
            ans.headline = "Nobody with recorded changes to this part of the code is in the history."
            return ans
        top = ans.ask[0]
        target = seeds[0]["name"]
        ans.status = "answered"
        ans.headline = (f"Ask {top['name']} about {target}: most recent substantive work there, "
                        f"last active in the repository on {top['last_active']}.")
        gone = [p for p in self.people_to_ask(entities, []) if not p["active"]] if entities else []
        if gone:
            ans.headline += f" ({gone[0]['name']} also knows it but has not committed since {gone[0]['last_active']}.)"
        recent = []
        for eid in entities[:2]:
            for did, _, _ in self.g.inc(eid, "ABOUT"):
                d = self.g.node(did)
                if self.g.lbl(did) == "Decision" and not d.get("low_signal") and d["rationale"]:
                    recent.append((d["date"], did))
        recent = [d for _, d in sorted(recent, reverse=True)[:3]]
        ans.related = [self._dec_card(d) for d in recent]
        ans.confidence, ans.confidence_label = 0.8, "high"
        ans.confidence_parts = {"basis": "commit authorship on the matched code, weighted to the last two years"}
        ans.subgraph = self._subgraph(seeds[:2], recent, ans.ask)
        return ans

    def _answer_code_history(self, ans: "Answer", seed, seeds) -> "Answer":
        """We know the code entity; say what the history records, and that the reason is missing."""
        g = self.g
        events = []
        for did, _, p in g.inc(seed["id"], "ABOUT"):
            if g.lbl(did) == "Decision" and p.get("change") in ("introduce", "set", "remove"):
                events.append((g.node(did)["date"], did, p))
        events.sort()
        ans.ask = self.people_to_ask([seed["id"]], [e[1] for e in events[-2:]])
        if not events:
            ans.headline = f"{seed['name']} exists, but I can't find the change that introduced it or a reason for it."
            return ans
        date0, d0, p0 = events[0]
        first = self._dec_card(d0)
        ans.current = first
        vals = []
        for dt, _, p in events:
            if p.get("value") and (not vals or vals[-1][1] != p.get("value")):
                vals.append((dt, p.get("value")))
        hist = "; ".join(f"{v} since {dt}" for dt, v in vals[-3:]) if vals else ""
        has_why = any(g.node(d)["rationale"] for _, d, _ in events)
        ans.status = "answered" if has_why else "what_not_why"
        who = ", ".join(p["name"] for p in first["people"][:1]) or "an unknown author"
        ans.headline = ""
        if vals:
            asked = premise_number(ans.question)
            current = vals[-1][1]
            if asked is not None and str(asked) != current.strip():
                ans.headline = f"Check the premise: {seed['name']} is {current}, not {asked}. "
        ans.headline += (f"{seed['name']} was introduced on {date0} in \"{first['title']}\" by {who}"
                         + (f" (value history: {hist})" if hist else "") + ". ")
        if events[-1][2].get("change") == "remove":
            last = self._dec_card(events[-1][1])
            lwho = ", ".join(p["name"] for p in last["people"][:1]) or "an unknown author"
            ans.headline += f"It was removed on {events[-1][0]} in \"{last['title']}\" by {lwho}. "
            ans.current = last
        if has_why:
            best = max((d for _, d, _ in events), key=lambda d: len(g.node(d)["rationale"]))
            ans.why = g.node(best)["rationale"][:3]
            ans.headline += "The reason recorded with that history is below."
        else:
            ans.headline += (f"No commit, pull request or release note records why. "
                             f"Ask {ans.ask[0]['name'] if ans.ask else 'the maintainers'}.")
        ans.chain = [{"date": dt, "decision": d, "title": g.node(d)["title"], "kind": g.node(d)["kind"],
                      "pr": g.node(d).get("pr"), "people": [x["name"] for x in self._dec_card(d)["people"]][:2],
                      "status": p.get("change"), "value": p.get("value")} for dt, d, p in events]
        ans.confidence = 0.75 if has_why else 0.4
        ans.confidence_label = "medium" if has_why else "low"
        ans.confidence_parts = {"entity link": seed["match"], "reason recorded": 1.0 if has_why else 0.0}
        ans.subgraph = self._subgraph([seed], [e[1] for e in events[:5]], ans.ask)
        return ans

    # ------------------------------------------------------------------ graph for the UI
    def _subgraph(self, seeds, decisions, people) -> dict:
        g = self.g
        nodes, edges = {}, []

        def add(nid, label=None, name=None, **extra):
            if nid not in nodes:
                nodes[nid] = {"id": nid, "label": label or g.lbl(nid), "name": name or self._short(nid), **extra}

        for s in seeds:
            add(s["id"], s["label"], s["name"], seed=True)
        for d in decisions:
            add(d, "Decision", self._short(d))
            for x, r, p in g.out(d, None):
                if r in ("DOCUMENTED_IN", "RESPONDS_TO", "MADE_IN"):
                    add(x)
                    edges.append({"source": d, "target": x, "type": r})
                elif r == "ABOUT" and (x in nodes or (g.lbl(x) in ("Symbol", "ConfigKey") and p.get("change") in ("introduce", "remove"))):
                    add(x)
                    edges.append({"source": d, "target": x, "type": r, "change": p.get("change")})
                elif r == "SUPERSEDES" and x in decisions:
                    edges.append({"source": d, "target": x, "type": r, "rule": p.get("rule")})
                elif r == "DECIDED_BY" and not g.node(x).get("is_bot"):
                    add(x)
                    edges.append({"source": d, "target": x, "type": r})
            for s in seeds:
                if any(x == s["id"] for x, _, _ in g.out(d, "ABOUT")):
                    edges.append({"source": d, "target": s["id"], "type": "ABOUT"})
        for p in people[:2]:
            add(p["id"], "Person", p["name"])
        # de-duplicate
        seen, uniq = set(), []
        for e in edges:
            k = (e["source"], e["target"], e["type"])
            if k not in seen and e["source"] in nodes and e["target"] in nodes:
                seen.add(k)
                uniq.append(e)
        return {"nodes": list(nodes.values())[:40], "edges": uniq[:80]}

    def _short(self, nid: str) -> str:
        g, n, l = self.g, self.g.node(nid), self.g.lbl(nid)
        if l == "Decision":
            return (f"#{n['pr']} " if n.get("pr") else "") + n["title"][:48]
        if l == "PullRequest":
            return f"PR #{n['number']}"
        if l == "Commit":
            return n["short"]
        if l == "ReleaseNote":
            return f"release {n['version']}"
        if l == "Person":
            return n["name"]
        if l == "Issue":
            return f"issue #{n['number']}"
        return self._name(nid)


CYPHER_WHY = """// The same question, as Kyun runs it on Neo4j / AuraDB
MATCH (e) WHERE e.key IN $keys OR e.name IN $names           // 1. link
MATCH (d:Decision)-[a:ABOUT|RESPONDS_TO]->(e)                 // 2. expand
OPTIONAL MATCH chain = (d)-[:SUPERSEDES*0..4]-(other:Decision) // 3. thread
WITH d, a, collect(DISTINCT other) AS thread
UNWIND thread AS t
WITH DISTINCT t
WHERE NOT EXISTS { MATCH (:Decision)-[:SUPERSEDES]->(t) }      //    live = nothing overrode it
OPTIONAL MATCH (t)-[:DOCUMENTED_IN]->(rn:ReleaseNote)
OPTIONAL MATCH (t)-[:DECIDED_BY]->(p:Person)
OPTIONAL MATCH (t)<-[s:SUPERSEDES]-(:Decision)
RETURN t.title, t.date, t.rationale, collect(DISTINCT rn.text) AS notes,
       collect(DISTINCT p.name) AS people"""
