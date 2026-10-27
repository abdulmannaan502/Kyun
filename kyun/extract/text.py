"""Small, explainable text rules: references, decision kinds, rationale, concepts.

These are deliberately rules, not a model. Every fact Kyun puts in the graph
has to be traceable back to a span of text a human can check.
"""
from __future__ import annotations

import re

PR_RE = re.compile(r"(?<![\w/&\\])#(\d{2,6})\b")
MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from ([\w.-]+)/(\S+)")
CLOSES_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|reported in|see|refs?)\s*:?\s*#(\d{2,6})", re.I)
CVE_RE = re.compile(r"\bCVE-\d{4}-\d{4,7}\b")
GHSA_RE = re.compile(r"\bGHSA(?:-[0-9a-z]{4}){3}\b", re.I)
REVERT_SHA_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
REVERT_SUBJ_RE = re.compile(r'^Revert "(.+)"\s*$')

KIND_RULES = [
    ("revert",     re.compile(r"^\s*revert|\breverts?\b|\breverting\b|\bback out\b|\brolled? back\b", re.I)),
    ("security",   re.compile(r"\bCVE-|\bGHSA-|\bsecurity\b|\bvulnerab|\bcredential leak", re.I)),
    ("deprecate",  re.compile(r"\bdeprecat", re.I)),
    ("remove",     re.compile(r"^\s*(remove|drop|delete|kill)\b|\bdropped support\b|\bno longer\b", re.I)),
    ("dependency", re.compile(r"\b(upgrade|bump|pin|vendor|unvendor|switch|replace|instead of)\b.*\b(to|with|for)\b|\bupper (limit|bound)\b|\brequire[sd]? \w+ ?[<>=]", re.I)),
    ("fix",        re.compile(r"^\s*(fix|fixes|fixed|bugfix|hotfix)\b|\bregression\b|\brace condition\b|\bcrash", re.I)),
    ("introduce",  re.compile(r"^\s*(add|adds|added|introduce|implement|support|allow|enable|use)\b", re.I)),
]

RATIONALE_RE = re.compile(
    r"\b(because|since|as a result|in order to|so that|so as to|to avoid|to prevent|to improve|"
    r"to reduce|to remove|to keep|to make|to allow|to provide|to ensure|to minimi[sz]e|which should|"
    r"due to|caused|causing|has created|was causing|breaks?|broke|broken|unsustainable|"
    r"license|licensing|backwards? compat|performance|security|vulnerab|regression|"
    r"we (?:have )?decided|the team (?:has )?decided|this means|the reason|otherwise|"
    r"instead of|rather than|negative impact|maintenance)\b", re.I)

LOW_SIGNAL_RE = re.compile(
    r"^(merge (branch|remote)|update (readme|history|docs?)|typo|fix typo|v?\d+\.\d+(\.\d+)?$|"
    r"bump version|release|wip|lint|format|whitespace|pep8|flake8|\.|updates?)\b", re.I)

BOT_RE = re.compile(r"\[bot\]|dependabot|github-actions|pre-commit-ci", re.I)

STOP = set("""a an the and or of to in on for with by from at as is are was were be been being it its this that
these those there here not no do does did why how what when who whom which where will would can could should
we our you your i me my they them their he she his her into than then so such too very also just only about
over under again more most some any each other all both few own same once until while does doesn't isn't
aren't wasn't weren't don't didn't won't use used using make made get got have has had is""".split())

IDENT_RE = re.compile(r"`([^`]{2,60})`|\b([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\b|"
                      r"\b([a-z]+_[a-z0-9_]+)\b|\b(_[a-z][a-z0-9_]+)\b|\b([A-Z][a-z]+[A-Z][A-Za-z0-9]*|[A-Z]{2,}[a-z][A-Za-z0-9]*)\b|"
                      r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]+)\b")


def pr_refs(text: str) -> set[int]:
    return {int(x) for x in PR_RE.findall(text)}


def closes_refs(text: str) -> set[int]:
    return {int(x) for x in CLOSES_RE.findall(text)}


def classify(subject: str, body: str = "") -> str:
    for kind, rx in KIND_RULES:
        if rx.search(subject):
            return kind
    for kind, rx in KIND_RULES[:3]:
        if rx.search(body):
            return kind
    return "change"


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`(])", text)
    return [p.strip() for p in parts if len(p.strip()) > 12]


def rationale_spans(text: str) -> list[str]:
    """Sentences that state a reason, not just an action."""
    out = []
    for s in sentences(text):
        if REVERT_SHA_RE.search(s) or s.lower().startswith(("signed-off-by", "co-authored-by")):
            continue
        if RATIONALE_RE.search(s):
            out.append(s[:400])
    return out


def is_low_signal(subject: str) -> bool:
    return bool(LOW_SIGNAL_RE.match(subject.strip()))


def split_ident(name: str) -> list[str]:
    """SSLContext -> [ssl, context]; _preloaded_ssl_context -> [preloaded, ssl, context]."""
    name = name.strip("`_ .")
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    return [p for p in re.split(r"[\s_.\-/]+", name.lower()) if p]


def ident_keys(name: str) -> set[str]:
    """All the spellings one identifier can be asked about by.

    The point is to join vocabularies: a person asks about "SSLContext",
    the code says `_preloaded_ssl_context`, the changelog says "SSL context".
    """
    parts = split_ident(name)
    keys = {"".join(parts)} if parts else set()
    for i in range(len(parts) - 1):
        keys.add(parts[i] + parts[i + 1])
    for i in range(len(parts) - 2):
        keys.add(parts[i] + parts[i + 1] + parts[i + 2])
    return {k for k in keys if len(k) >= 4}


def concepts(text: str) -> set[str]:
    """Identifier-like terms mentioned in prose (`code`, dotted.names, CamelCase, snake_case)."""
    found = set()
    for m in IDENT_RE.finditer(text or ""):
        tok = next(g for g in m.groups() if g)
        tok = tok.strip().strip("()`'\".,:")
        if 2 < len(tok) <= 60 and " " not in tok:
            found.add(tok)
    for c in CVE_RE.findall(text or ""):
        found.add(c.upper())
    return found


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9_]+", (text or "").lower()) if w not in STOP and len(w) > 2]


def note_rationale(text: str) -> list[str]:
    """In a release note the first sentence says what changed; what follows explains it."""
    sents = sentences(text)
    out = [x for x in sents[:1] if RATIONALE_RE.search(x)]
    out += [x for x in sents[1:] if not re.match(r"^(see|refs?|thanks|\(#)", x, re.I)]
    for x in rationale_spans(text):
        if x not in out:
            out.append(x)
    if not out and sents and RATIONALE_RE.search(sents[0]):
        out = [sents[0]]
    return [x[:400] for x in out]


NUM_TERM_RE = re.compile(r"\b([a-z][a-z]+)[ -]?v?(\d+)(?:\.\d+)*\b", re.I)


def number_terms(text: str) -> set[str]:
    """'Python 2.7' -> python2, 'HTTP/1.1' handled elsewhere; keeps major versions searchable."""
    return {f"{w.lower()}{n}" for w, n in NUM_TERM_RE.findall(text or "") if w.lower() not in STOP}
