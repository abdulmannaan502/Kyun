"""Turn a repository's history into a graph of decisions moving through time."""
from __future__ import annotations

import os
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime

from ..extract import text as tx
from ..ingest.changelog import find_changelog, parse_changelog
from ..ingest.git_log import excluded, read_commits, read_python_patches
from ..ingest.github_api import load_cached_threads
from ..resolve.entities import PeopleResolver, canonical_paths, module_label
from ..temporal.supersession import detect_supersession
from .store import MemoryGraph

SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


def _date(iso: str) -> str:
    return (iso or "")[:10]


def _is_test(path: str) -> bool:
    return bool(re.search(r"(^|/)(tests?|testing)/|(^|/)test_[^/]+\.py$|_test\.py$|conftest\.py$", path))


def _pr_members(repo: str, merges: list) -> dict[str, list[str]]:
    out = {}
    for c in merges:
        if len(c.parents) < 2:
            continue
        r = subprocess.run(["git", "-C", repo, "rev-list", "--max-count=300",
                            f"{c.parents[0]}..{c.parents[1]}"], capture_output=True, text=True)
        out[c.sha] = r.stdout.split()
    return out


def build_graph(repo: str, name: str | None = None, github_cache: str | None = None,
                log=print) -> MemoryGraph:
    t0 = time.time()
    name = name or os.path.basename(os.path.abspath(repo))
    g = MemoryGraph()

    log(f"[1/7] reading git history of {name} ...")
    commits = read_commits(repo)
    read_python_patches(repo, commits)
    by_sha = {c.sha: c for c in commits}
    log(f"      {len(commits)} commits")

    # ---- files: follow renames to one canonical id ---------------------------
    log("[2/7] resolving files, symbols and people ...")
    renames = [(f.old_path, f.path) for c in commits for f in c.files if f.status == "R" and f.old_path]
    canon_map = canonical_paths(renames)
    canon = lambda p: canon_map.get(p, p)
    aliases = defaultdict(set)
    for old, new in renames:
        aliases[canon(new)].update({old, new})

    def file_node(path: str) -> str | None:
        if excluded(path):
            return None
        cp = canon(path)
        return g.add_node(f"file:{cp}", "File", path=cp, module=module_label(cp) if cp.endswith(".py") else None,
                          aliases=sorted(aliases.get(cp, {cp}) - {cp}), is_test=_is_test(cp))

    # ---- people ---------------------------------------------------------------
    people = PeopleResolver()
    for c in commits:
        people.add(c.author_name, c.author_email)
    people.finalize()
    person_stats = defaultdict(lambda: {"commits": 0, "first": "9999", "last": "0000"})
    for c in commits:
        pid = people.person_id(c.author_name, c.author_email)
        s = person_stats[pid]
        s["commits"] += 1
        s["first"] = min(s["first"], _date(c.committed))
        s["last"] = max(s["last"], _date(c.committed))
    for pid, s in person_stats.items():
        disp, names, emails, handles = people.display(pid)
        g.add_node(pid, "Person", name=disp, names=names, emails=emails, handles=handles,
                   commits=s["commits"], first_active=s["first"], last_active=s["last"],
                   is_bot=bool(tx.BOT_RE.search(disp + " ".join(emails))))

    # ---- commits, symbols, config keys ----------------------------------------
    defined: dict[str, str] = {}     # symbol/config id -> last value (existence tracking)
    for c in commits:
        pid = people.person_id(c.author_name, c.author_email)
        g.add_node(f"commit:{c.sha}", "Commit", sha=c.sha, short=c.sha[:8], subject=c.subject,
                   message=c.message[:3000], date=_date(c.committed), authored=c.authored,
                   is_merge=c.is_merge, added=c.lines_added, removed=c.lines_removed)
        g.add_edge(pid, "AUTHORED", f"commit:{c.sha}")
        for f in c.files:
            fid = file_node(f.path)
            if fid:
                g.add_edge(f"commit:{c.sha}", "TOUCHES", fid, status=f.status)
        for key in c.touched_symbols:
            path, sym = key.split("::", 1)
            if excluded(path):
                continue
            cp = canon(path)
            fid = file_node(path)
            is_cfg = sym.isupper()
            nid = f"{'cfg' if is_cfg else 'sym'}:{cp}::{sym}"
            g.add_node(nid, "ConfigKey" if is_cfg else "Symbol", name=sym, path=cp,
                       module=module_label(cp), is_test=_is_test(cp))
            if fid:
                g.add_edge(nid, "DEFINED_IN", fid)
            if key in c.added_defs or key in c.lifecycle_new:
                kind = "introduce"
            elif key in c.removed_defs or key in c.const_removed:
                kind = "remove"
            else:
                kind = "edit"
            g.add_edge(f"commit:{c.sha}", "TOUCHES", nid, kind=kind)
            if key in c.const_values:
                val = c.const_values[key]
                if is_cfg:
                    g.add_edge(f"commit:{c.sha}", "SETS", nid, value=val, previous=defined.get(nid))
                defined[nid] = val
            elif kind == "introduce":
                defined[nid] = "def"
            elif kind == "remove":
                defined.pop(nid, None)

    # ---- pull requests ----------------------------------------------------------
    log("[3/7] grouping commits into pull requests ...")
    merges = [c for c in commits if c.is_merge]
    members = _pr_members(repo, merges)
    commit_pr: dict[str, int] = {}
    pr_merge: dict[int, str] = {}
    for c in merges:
        m = tx.MERGE_PR_RE.match(c.subject)
        if not m:
            continue
        n = int(m.group(1))
        body = [ln for ln in c.message.splitlines()[1:] if ln.strip()]
        title = body[0].strip() if body else m.group(3).replace("-", " ").replace("_", " ")
        g.add_node(f"pr:{n}", "PullRequest", number=n, title=title, branch=f"{m.group(2)}/{m.group(3)}",
                   merged=_date(c.committed), merge_sha=c.sha, style="merge")
        g.add_edge(f"commit:{c.sha}", "PART_OF", f"pr:{n}", role="merge")
        pr_merge[n] = c.sha
        commit_pr[c.sha] = n
        for s in members.get(c.sha, []):
            if s in by_sha and s not in commit_pr:
                commit_pr[s] = n
                g.add_edge(f"commit:{s}", "PART_OF", f"pr:{n}", role="commit")
    for c in commits:
        if c.is_merge or c.sha in commit_pr:
            continue
        m = SQUASH_PR_RE.search(c.subject)
        if m:
            n = int(m.group(1))
            g.add_node(f"pr:{n}", "PullRequest", number=n, title=SQUASH_PR_RE.sub("", c.subject).strip(),
                       merged=_date(c.committed), merge_sha=c.sha, style="squash")
            commit_pr[c.sha] = n
            g.add_edge(f"commit:{c.sha}", "PART_OF", f"pr:{n}", role="squash")

    threads = load_cached_threads(github_cache, *name.split("/", 1)) if github_cache and "/" in name else {}

    # ---- decisions ----------------------------------------------------------------
    log("[4/7] extracting decisions and their rationale ...")
    units: dict[str, list] = defaultdict(list)
    for c in commits:
        if c.sha in commit_pr:
            units[f"pr:{commit_pr[c.sha]}"].append(c)
        elif not c.is_merge:
            units[f"commit:{c.sha}"].append(c)

    concept_df = defaultdict(set)

    def attach_concepts(did: str, text: str, title: str):
        for tok in tx.concepts(text):
            parts = tx.split_ident(tok)
            key = "".join(parts)
            if len(key) < 4 or key in tx.STOP:
                continue
            cid = f"concept:{key}"
            node = g.nodes.get(cid)
            forms = set(node["forms"]) if node else set()
            forms.add(tok)
            g.add_node(cid, "Concept", key=key, forms=sorted(forms)[:12])
            g.add_edge(did, "ABOUT", cid, in_title=tok in title, weight=1.0 if tok in title else 0.7)
            concept_df[key].add(did)

    for uid, cs in units.items():
        cs = sorted(cs, key=lambda c: c.committed)
        is_pr = uid.startswith("pr:")
        pr = g.node(uid) if is_pr else None
        title = pr["title"] if is_pr else cs[-1].subject
        msgs = "\n\n".join(c.message for c in cs)
        if is_pr and pr.get("merge_sha") in by_sha:
            msgs = by_sha[pr["merge_sha"]].message + "\n\n" + msgs
        date = pr["merged"] if is_pr else _date(cs[-1].committed)
        kind = tx.classify(title, msgs)
        rationale = [{"text": s, "source": "commit message", "ref": uid} for s in tx.rationale_spans(msgs)]
        num = pr["number"] if is_pr else None
        thread = threads.get(num) if num else None
        if thread:
            tid = f"discussion:{num}"
            g.add_node(tid, "Discussion", number=num, title=thread.title, url=thread.url,
                       author=thread.author, comments=len(thread.comments))
            for s in tx.rationale_spans(thread.body)[:6]:
                rationale.append({"text": s, "source": "PR description", "ref": tid})
            for cm in thread.comments:
                for s in tx.rationale_spans(cm["body"])[:2]:
                    rationale.append({"text": s, "source": f"comment by @{cm['author']}", "ref": tid})
        low = all(tx.is_low_signal(c.subject) for c in cs) and not rationale
        bot = all(tx.BOT_RE.search(c.author_name + c.author_email) for c in cs)
        did = f"dec:{uid}"
        g.add_node(did, "Decision", title=title, date=date, kind=kind, rationale=rationale[:12],
                   low_signal=low or bot, source="pull request" if is_pr else "commit",
                   shas=[c.sha for c in cs][:40], pr=num, text=msgs[:4000])
        g.add_edge(did, "MADE_IN", uid)
        if thread:
            g.add_edge(did, "DISCUSSED_IN", f"discussion:{num}")
        for c in cs:
            g.add_edge(did, "DECIDED_BY", people.person_id(c.author_name, c.author_email))
            if not is_pr:
                pass
            # what the decision is about, from the code it changed
            for dst, rel, p in list(g.out(f"commit:{c.sha}", "TOUCHES")):
                lab = g.lbl(dst)
                if lab in ("Symbol", "ConfigKey"):
                    w = {"introduce": 1.0, "remove": 1.0}.get(p.get("kind"), 0.6)
                    g.add_edge(did, "ABOUT", dst, via="code", change=p.get("kind"), weight=w)
                elif lab == "File":
                    g.add_edge(did, "ABOUT", dst, via="code", change=p.get("status"),
                               weight=round(1.0 / max(1, len(c.files)), 3))
            for dst, rel, p in list(g.out(f"commit:{c.sha}", "SETS")):
                g.add_edge(did, "ABOUT", dst, via="value", value=p.get("value"),
                           previous=p.get("previous"), weight=1.0, change="set")
        attach_concepts(did, title + "\n" + msgs + "\n" + (thread.body if thread else ""), title)
        # incidents and issues
        for cve in set(tx.CVE_RE.findall(msgs)) | {x.upper() for x in tx.GHSA_RE.findall(msgs)}:
            g.add_node(f"incident:{cve.upper()}", "Incident", ref=cve.upper())
            g.add_edge(did, "RESPONDS_TO", f"incident:{cve.upper()}")
        for n in tx.closes_refs(msgs) | (tx.pr_refs(title) - ({num} if num else set())):
            if f"pr:{n}" in g.nodes:
                g.add_edge(did, "REFERENCES", f"pr:{n}")
            else:
                g.add_node(f"issue:{n}", "Issue", number=n)
                g.add_edge(did, "RESPONDS_TO", f"issue:{n}")

    # ---- changelog ---------------------------------------------------------------------
    log("[5/7] reading the changelog ...")
    cl = find_changelog(repo)
    notes = parse_changelog(cl) if cl else []
    for n in notes:          # "Unreleased" sections are dated by the newest commit
        if not n.date:
            n.date = _date(commits[-1].committed)
    releases = sorted({(n.version, n.date) for n in notes}, key=lambda x: x[1])
    prev_date = {}
    for i, (v, d) in enumerate(releases):
        prev_date[v] = releases[i - 1][1] if i else "0000-00-00"
        g.add_node(f"release:{v}", "Release", version=v, date=d)
    decisions_by_date = sorted((g.node(d)["date"], d) for d in g.by_label("Decision"))
    # who wrote each release note: one blame of the changelog
    blame: dict[int, str] = {}
    if cl:
        r = subprocess.run(["git", "-C", repo, "blame", "--line-porcelain", "--", os.path.basename(cl)],
                           capture_output=True, text=True, errors="replace")
        line_no, author = 0, ""
        for ln in r.stdout.splitlines():
            m = re.match(r"^[0-9a-f]{40} \d+ (\d+)", ln)
            if m:
                line_no = int(m.group(1))
            elif ln.startswith("author "):
                author = ln[7:]
            elif ln.startswith("author-mail "):
                email = ln[12:].strip("<>")
                pid = people.person_id(author, email)
                if pid in g.nodes:
                    blame[line_no] = pid
    documented = 0
    for n in notes:
        g.add_node(n.id, "ReleaseNote", version=n.version, date=n.date, section=n.section,
                   text=n.text, refs=n.refs, file=n.source_file, line=n.line)
        g.add_edge(n.id, "IN_RELEASE", f"release:{n.version}")
        targets = []
        for r in n.refs:
            if f"dec:pr:{r}" in g.nodes:
                targets.append((f"dec:pr:{r}", "pr reference"))
            elif f"issue:{r}" in g.nodes:
                targets += [(s, "fixes referenced issue") for s, _, _ in g.inc(f"issue:{r}", "RESPONDS_TO")]
        if not targets:
            # infer: a decision that landed in this release window and shares a specific concept
            keys = {"".join(tx.split_ident(t)) for t in tx.concepts(n.text)}
            keys = {k for k in keys if len(k) >= 5 and len(concept_df.get(k, ())) <= 40}
            kind = tx.classify(n.text)
            best, best_ov = None, 0
            for dd, did in decisions_by_date:
                if not (prev_date.get(n.version, "") < dd <= n.date):
                    continue
                dkeys = {dst.split(":", 1)[1] for dst, _, _ in g.out(did, "ABOUT") if dst.startswith("concept:")}
                ov = len(keys & dkeys) + (0.5 if g.node(did)["kind"] == kind else 0)
                if keys & dkeys and ov > best_ov:
                    best, best_ov = did, ov
            if best:
                targets.append((best, "inferred: same release window and shared concept"))
        incidents = {c.upper() for c in tx.CVE_RE.findall(n.text)} | {x.upper() for x in tx.GHSA_RE.findall(n.text)}
        for ref in incidents:
            g.add_node(f"incident:{ref}", "Incident", ref=ref)
        if targets:
            for did, how in targets:
                g.add_edge(did, "DOCUMENTED_IN", n.id, how=how)
                for ref in incidents:
                    g.add_edge(did, "RESPONDS_TO", f"incident:{ref}", via="release notes")
                node = g.node(did)
                for s in tx.note_rationale(n.text):
                    node["rationale"].append({"text": s, "source": f"release notes {n.version}", "ref": n.id})
                attach_concepts(did, n.text, "")
            documented += 1
        else:
            did = f"dec:{n.id}"
            g.add_node(did, "Decision", title=n.text[:140], date=n.date, kind=tx.classify(n.text),
                       rationale=[{"text": s, "source": f"release notes {n.version}", "ref": n.id}
                                  for s in tx.note_rationale(n.text)],
                       low_signal=False, source="release notes", shas=[], pr=None, text=n.text)
            g.add_edge(did, "DOCUMENTED_IN", n.id, how="release note only")
            if n.line in blame:
                g.add_edge(did, "DECIDED_BY", blame[n.line], role="wrote the release note")
            for ref in incidents:
                g.add_edge(did, "RESPONDS_TO", f"incident:{ref}", via="release notes")
            attach_concepts(did, n.text, n.text)
    log(f"      {len(notes)} release notes, {documented} tied to code decisions")

    # the same concept written as plain prose ("chardet", "ssl context") joins the graph too
    concept_keys = {g.node(c)["key"] for c in g.by_label("Concept")}
    for did in g.by_label("Decision"):
        d = g.node(did)
        title_w = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]+", d["title"])]
        body_w = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_]+", d.get("text") or "")]
        for words, in_title in ((title_w, True), (body_w[:400], False)):
            grams = set(words) | {a + b for a, b in zip(words, words[1:])}
            for k in grams:
                k2 = k.replace("_", "")
                if len(k2) >= 5 and k2 in concept_keys and k2 not in tx.STOP:
                    cid = f"concept:{k2}"
                    if not any(dst == cid for dst, _, _ in g.out(did, "ABOUT")):
                        g.add_edge(did, "ABOUT", cid, in_title=in_title, weight=0.8 if in_title else 0.5, via="prose")

    # concepts that name a symbol or config key point to it
    sym_by_key = defaultdict(list)
    for sid in g.by_label("Symbol") + g.by_label("ConfigKey"):
        sym_by_key["".join(tx.split_ident(g.node(sid)["name"]))].append(sid)
    for cid in g.by_label("Concept"):
        for sid in sym_by_key.get(g.node(cid)["key"], []):
            g.add_edge(cid, "REFERENCES", sid)

    # one reason, stated once
    for did in g.by_label("Decision"):
        node, seen, keep = g.node(did), set(), []
        title_key = re.sub(r"\W+", "", node["title"].lower())
        for r in node["rationale"]:
            k = re.sub(r"\W+", "", r["text"].lower())
            if k in seen or k == title_key or (k.startswith(title_key) and len(k) < len(title_key) + 20):
                continue
            seen.add(k)
            keep.append(r)
        node["rationale"] = keep[:10]

    # what still exists at HEAD (so "knowledge at risk" is about live code)
    from ..ingest.git_log import _Blobs, _defines
    head_files = set(subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True, text=True).stdout.split())
    blobs = _Blobs(repo)
    try:
        for fid in g.by_label("File"):
            g.node(fid)["alive"] = g.node(fid)["path"] in head_files
        for sid in g.by_label("Symbol") + g.by_label("ConfigKey"):
            n = g.node(sid)
            n["alive"] = n["path"] in head_files and _defines(blobs.get("HEAD", n["path"]), n["name"])
    finally:
        blobs.close()

    log("[6/7] detecting superseded decisions ...")
    n_sup = detect_supersession(g, commits)
    log(f"      {n_sup} supersession links")

    log("[7/7] done")
    g.meta = {"repo": name, "built": datetime.now().isoformat(timespec="seconds"),
              "commits": len(commits), "head": commits[-1].sha if commits else None,
              "first_commit": _date(commits[0].committed) if commits else None,
              "last_commit": _date(commits[-1].committed) if commits else None,
              "changelog": os.path.basename(cl) if cl else None,
              "seconds": round(time.time() - t0, 1)}
    return g
