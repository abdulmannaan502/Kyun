"""Parse an in-repo changelog (HISTORY.md / CHANGELOG.md / CHANGES.rst / NEWS).

Release notes are the closest thing most projects have to design records:
maintainers write *why* a behaviour changed, attach PR numbers, and say
when an earlier change was reverted. Each bullet becomes a ReleaseNote.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

CANDIDATES = ("HISTORY.md", "CHANGELOG.md", "CHANGES.md", "HISTORY.rst",
              "CHANGELOG.rst", "CHANGES.rst", "NEWS.md", "NEWS.rst", "NEWS")

VERSION_RE = re.compile(r"^v?(\d+\.\d+(?:\.\d+)?(?:[a-z0-9.]*)?)\s*\(?(\d{4}-\d{2}-\d{2})\)?\s*$")
UNDERLINE_RE = re.compile(r"^[-=+~^*]{3,}\s*$")
SECTION_RE = re.compile(r"^\s*(?:\*\*|#{2,4}\s*)([A-Za-z][A-Za-z /&-]+?)(?:\*\*)?:?\s*$")
BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
VERSION_WORD_RE = re.compile(r"^(?:Version|Release|v)\s+(\d+\.\d+(?:\.\d+)?[a-z0-9.]*)\s*$", re.I)
RELEASED_RE = re.compile(r"^(?:Released(?: on)?:?\s*)?(\d{4}-\d{2}-\d{2})\b|^Unreleased\b", re.I)
ROLE_REF_RE = re.compile(r":(?:pr|issue|ghissue|gh):`#?(\d+)`")
PR_RE = re.compile(r"(?<![\w/\\])#(\d{2,6})\b")
GH_URL_RE = re.compile(r"github\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/(\d+)")


@dataclass
class ReleaseNote:
    id: str
    version: str
    date: str
    section: str
    text: str
    refs: list[int] = field(default_factory=list)
    source_file: str = ""
    line: int = 0


def find_changelog(repo: str) -> str | None:
    for name in CANDIDATES:
        p = os.path.join(repo, name)
        if os.path.isfile(p):
            return p
    return None


def parse_changelog(path: str) -> list[ReleaseNote]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        lines = fh.read().splitlines()
    notes: list[ReleaseNote] = []
    version = date = None
    section = "Changes"
    cur: list[str] = []
    cur_line = 0

    def flush():
        nonlocal cur
        if cur and version:
            text = " ".join(s.strip() for s in cur).strip()
            text = re.sub(r"\s+", " ", text)
            refs = sorted({int(x) for x in PR_RE.findall(text)} |
                          {int(x) for x in GH_URL_RE.findall(text)} |
                          {int(x) for x in ROLE_REF_RE.findall(text)})
            notes.append(ReleaseNote(
                id=f"rn:{version}:{len(notes)}", version=version, date=date or "",
                section=section, text=text, refs=refs,
                source_file=os.path.basename(path), line=cur_line))
        cur = []

    in_code = False
    for i, ln in enumerate(lines, 1):
        if ln.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        m = VERSION_RE.match(ln.strip().lstrip("#").strip())
        if m:
            flush()
            version, date, section = m.group(1), m.group(2), "Changes"
            continue
        m = VERSION_WORD_RE.match(ln.strip().lstrip("#").strip())
        if m:
            flush()
            version, date, section = m.group(1), "", "Changes"
            # the date sits on one of the next few lines: "Released 2021-05-11"
            for nxt in lines[i:i + 4]:
                r = RELEASED_RE.match(nxt.strip())
                if r:
                    date = r.group(1) or ""
                    break
            continue
        if RELEASED_RE.match(ln.strip()) and not cur:
            continue
        if UNDERLINE_RE.match(ln):
            continue
        s = SECTION_RE.match(ln)
        if s and not BULLET_RE.match(ln):
            flush()
            section = s.group(1).strip()
            continue
        b = BULLET_RE.match(ln)
        if b and len(b.group(1)) <= 1:
            flush()
            cur, cur_line = [b.group(2)], i
            continue
        if ln.strip() == "":
            continue
        if cur:
            cur.append(ln)
    flush()
    return notes
