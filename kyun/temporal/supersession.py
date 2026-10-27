"""Find decisions that a later decision quietly overrode.

A reversed decision still sits in the archive looking authoritative. These
rules link the later decision to the earlier one with a SUPERSEDES edge and
record *why* Kyun believes it (the evidence is shown to the user):

  1. explicit git revert          "This reverts commit <sha>"
  2. revert by subject            Revert "<subject of an earlier commit>"
  3. code lifecycle               the later change deletes the functions /
                                  module state the earlier change introduced
  4. changelog statement          "... originally introduced in 2.32.0 ...
                                  decided to revert"
  5. configuration value          a later change sets the same config key to
                                  a different value
  6. successive versions          "Increase chardet upper limit to 7" is
                                  replaced by "... to 8"
"""
from __future__ import annotations

import re
from collections import defaultdict

from ..extract import text as tx

INTRODUCED_IN_RE = re.compile(r"(?:originally |first )?(?:introduced|added|shipped) in (?:v|version )?(\d+\.\d+(?:\.\d+)?)", re.I)
BUMP_RE = re.compile(r"\b(increase|bump|upgrade|update|raise|lower|pin|require|limit|bound|minimum|maximum)\b", re.I)
REVERT_WORD_RE = re.compile(r"\brevert|\broll(?:ed)? back|\bremov|\bundo", re.I)


def _decision_of_commit(g, sha_prefix: str, index: dict[str, str]) -> str | None:
    for sha, did in index.items():
        if sha.startswith(sha_prefix):
            return did
    return None


def _link(g, newer: str, older: str, rule: str, evidence: str) -> bool:
    if newer == older:
        return False
    a, b = g.node(newer), g.node(older)
    if not a or not b or a.get("date", "") < b.get("date", ""):
        return False
    return g.add_edge(newer, "SUPERSEDES", older, rule=rule, evidence=evidence)


def detect_supersession(g, commits) -> int:
    n = 0
    # commit sha -> decision id
    sha_dec: dict[str, str] = {}
    for did in g.by_label("Decision"):
        for sha in g.node(did).get("shas", []):
            sha_dec[sha] = did
    for did in g.by_label("Decision"):
        for dst, _, _ in g.out(did, "MADE_IN"):
            node = g.node(dst)
            if node.get("merge_sha"):
                sha_dec.setdefault(node["merge_sha"], did)
    subj_dec: dict[str, str] = {}
    for c in commits:
        if c.sha in sha_dec:
            subj_dec[c.subject.strip()] = sha_dec[c.sha]

    # 1 + 2: explicit reverts
    for c in commits:
        newer = sha_dec.get(c.sha)
        if not newer:
            continue
        for prefix in tx.REVERT_SHA_RE.findall(c.message):
            older = sha_dec.get(prefix) or _decision_of_commit(g, prefix, sha_dec)
            if older and _link(g, newer, older, "git revert", f"commit {c.sha[:8]} says: This reverts commit {prefix[:8]}"):
                n += 1
        m = tx.REVERT_SUBJ_RE.match(c.subject)
        if m:
            older = subj_dec.get(m.group(1).strip())
            if older and _link(g, newer, older, "revert by subject", f'commit {c.sha[:8]}: Revert "{m.group(1)[:80]}"'):
                n += 1

    # 3: code lifecycle -- a later decision deletes what an earlier one introduced
    introduced_by: dict[str, list[tuple[str, str]]] = defaultdict(list)   # symbol -> [(date, decision)]
    removed_by: dict[str, list[tuple[str, str]]] = defaultdict(list)
    intro_count: dict[str, int] = defaultdict(int)
    for did in g.by_label("Decision"):
        d = g.node(did)
        for dst, _, p in g.out(did, "ABOUT"):
            if g.lbl(dst) not in ("Symbol", "ConfigKey") or g.node(dst).get("is_test"):
                continue
            if p.get("change") == "introduce":
                introduced_by[dst].append((d["date"], did))
                intro_count[did] += 1
            elif p.get("change") == "remove":
                removed_by[dst].append((d["date"], did))
    pair_syms: dict[tuple[str, str], list[str]] = defaultdict(list)
    for sym, removals in removed_by.items():
        for rdate, rdid in removals:
            prior = [(d, i) for d, i in introduced_by.get(sym, []) if d <= rdate and i != rdid]
            if prior:
                _, idid = max(prior)
                pair_syms[(rdid, idid)].append(sym)
    for (newer, older), syms in pair_syms.items():
        k = g.node(newer).get("kind")
        share = len(syms) / max(1, intro_count[older])
        names = ", ".join(sorted(g.node(s)["name"] for s in syms)[:4])
        title = g.node(newer)["title"]
        rn = re.search(r"\b(?:move|rename)d?\s+`?(\w+)`?\s+(?:to|as|into)\s+`?(\w+)", title, re.I)
        if rn and any(g.node(s)["name"] == rn.group(1) for s in syms):
            if _link(g, newer, older, "rename",
                     f"renames {rn.group(1)} (introduced by the earlier change) to {rn.group(2)}"):
                n += 1
        elif k in ("revert", "remove", "deprecate") or share >= 0.5:
            if _link(g, newer, older, "code lifecycle",
                     f"removes {names} introduced by the earlier change ({len(syms)}/{intro_count[older]} of what it added)"):
                n += 1

    # 4: changelog statements: "<feature> originally introduced in X ... decided to revert"
    release_decisions: dict[str, list[str]] = defaultdict(list)
    for rn in g.by_label("ReleaseNote"):
        for did, _, _ in g.inc(rn, "DOCUMENTED_IN"):
            release_decisions[g.node(rn)["version"]].append(did)
    for rn in g.by_label("ReleaseNote"):
        text = g.node(rn)["text"]
        m = INTRODUCED_IN_RE.search(text)
        if not (m and REVERT_WORD_RE.search(text)):
            continue
        keys = {"".join(tx.split_ident(t)) for t in tx.concepts(text)}
        newer_list = [d for d, _, _ in g.inc(rn, "DOCUMENTED_IN")]
        for older in release_decisions.get(m.group(1), []):
            okeys = {dst.split(":", 1)[1] for dst, _, _ in g.out(older, "ABOUT") if dst.startswith("concept:")}
            shared = keys & okeys
            if not shared:
                continue
            for newer in newer_list:
                if _link(g, newer, older, "changelog statement",
                         f"release notes {g.node(rn)['version']}: originally introduced in {m.group(1)} and reverted (shared concept: {sorted(shared)[0]})"):
                    n += 1

    # 5: configuration values
    for key in g.by_label("ConfigKey"):
        sets = sorted((g.node(d)["date"], d, p.get("value")) for d, _, p in g.inc(key, "ABOUT") if p.get("change") == "set")
        for (d1, older, v1), (d2, newer, v2) in zip(sets, sets[1:]):
            if v1 != v2 and _link(g, newer, older, "config value",
                                  f"{g.node(key)['name']} changed from {v1} to {v2}"):
                n += 1
    # 6: successive versions of the same change ("... to 7" then "... to 8")
    families: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for did in g.by_label("Decision"):
        d = g.node(did)
        title = re.sub(r"\(?#\d+\)?", "", d["title"]).strip()
        if d.get("low_signal") or not re.search(r"\d", title) or not BUMP_RE.search(title):
            continue
        shape = re.sub(r"\d+(?:\.\d+)*", "#", title.lower()).strip()
        if len(shape) >= 12:
            families[shape].append((d["date"], did))
    for shape, members in families.items():
        members.sort()
        for (d1, older), (d2, newer) in zip(members, members[1:]):
            if g.node(older)["title"] != g.node(newer)["title"] and _link(
                    g, newer, older, "successive version",
                    f'"{g.node(newer)["title"][:60]}" replaces "{g.node(older)["title"][:60]}"'):
                n += 1
    return n
