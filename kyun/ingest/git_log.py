"""Read a repository's full history straight from git.

Two passes:
  1. commits + touched files + renames  (git log --name-status -M)
  2. python patches with function context (git log -p -U0, diff=python)
     -> which functions/classes/constants each commit touched, introduced
        or removed, and the values module-level constants were set to.

Nothing here needs network access; it works on any local clone.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

RS, US = "\x1e", "\x1f"

DEF_RE = re.compile(r"^\s*(?:async\s+)?(def|class)\s+([A-Za-z_]\w*)")
CONST_RE = re.compile(r"^([A-Z][A-Z0-9_]{2,}|_[a-z][a-z0-9_]{3,})\s*(?::[^=]+)?=\s*(.+)$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@\s?(.*)$")

DEFAULT_EXCLUDES = ("/packages/", "vendor/", "third_party/", "node_modules/")


@dataclass
class FileChange:
    status: str          # A, M, D, R
    path: str            # path after the change
    old_path: str | None = None


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author_name: str
    author_email: str
    authored: str        # ISO-8601
    committed: str
    message: str
    files: list[FileChange] = field(default_factory=list)
    # filled by the patch pass
    touched_symbols: set[str] = field(default_factory=set)      # "path::name"
    added_defs: set[str] = field(default_factory=set)
    removed_defs: set[str] = field(default_factory=set)
    const_values: dict[str, str] = field(default_factory=dict)  # "path::NAME" -> new value
    const_removed: set[str] = field(default_factory=set)
    lines_added: int = 0
    lines_removed: int = 0
    lifecycle_new: set[str] = field(default_factory=set)

    @property
    def subject(self) -> str:
        return self.message.splitlines()[0].strip() if self.message else ""

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1


def _git(repo: str, *args: str, attributes: str | None = None) -> str:
    cmd = ["git", "-C", repo, "-c", "core.quotepath=off"]
    if attributes:
        cmd += ["-c", f"core.attributesFile={attributes}"]
    cmd += list(args)
    out = subprocess.run(cmd, capture_output=True, check=True)
    return out.stdout.decode("utf-8", errors="replace")


def excluded(path: str, excludes=DEFAULT_EXCLUDES) -> bool:
    p = "/" + path
    return any(x in p for x in excludes)


def read_commits(repo: str, max_count: int | None = None) -> list[Commit]:
    fmt = f"{RS}%H{US}%P{US}%an{US}%ae{US}%aI{US}%cI{US}%B{US}"
    args = ["log", "--reverse", "-M", "--name-status", f"--format={fmt}"]
    if max_count:
        args.insert(1, f"--max-count={max_count}")
    raw = _git(repo, *args)
    commits: list[Commit] = []
    for rec in raw.split(RS):
        if not rec.strip():
            continue
        parts = rec.split(US)
        if len(parts) < 8:
            continue
        sha, parents, an, ae, ad, cd, body, files_blob = parts[:8]
        c = Commit(sha=sha, parents=parents.split(), author_name=an.strip(),
                   author_email=ae.strip(), authored=ad, committed=cd,
                   message=body.strip())
        for line in files_blob.strip().splitlines():
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            st = cols[0]
            if st.startswith("R") and len(cols) >= 3:
                c.files.append(FileChange("R", cols[2], cols[1]))
            else:
                c.files.append(FileChange(st[0], cols[1]))
        commits.append(c)
    return commits


def read_python_patches(repo: str, commits: list[Commit], excludes=DEFAULT_EXCLUDES) -> None:
    """Annotate commits in-place with symbol-level facts from their python diffs."""
    by_sha = {c.sha: c for c in commits}
    with tempfile.NamedTemporaryFile("w", suffix=".gitattributes", delete=False) as fh:
        fh.write("*.py diff=python\n")
        attrs = fh.name
    try:
        raw = _git(repo, "log", "--reverse", "-M", "-p", "-U0", "--no-color",
                   f"--format={RS}%H", "--", "*.py", attributes=attrs)
    finally:
        os.unlink(attrs)

    for rec in raw.split(RS):
        if not rec.strip():
            continue
        lines = rec.split("\n")
        c = by_sha.get(lines[0].strip())
        if c is None:
            continue
        path = old = None
        skip = False
        for ln in lines[1:]:
            if ln.startswith("diff --git"):
                path, old, skip = None, None, False
                continue
            if ln.startswith("--- "):
                p = ln[4:].strip()
                old = p[2:] if p.startswith("a/") else None
                continue
            if ln.startswith("+++ "):
                p = ln[4:].strip()
                path = p[2:] if p.startswith("b/") else old   # deleted file keeps its old path
                skip = path is None or excluded(path, excludes)
                continue
            if skip:
                continue
            if ln.startswith("@@"):
                m = HUNK_RE.match(ln)
                if m and path:
                    d = DEF_RE.search(m.group(5) or "")
                    if d:
                        c.touched_symbols.add(f"{path}::{d.group(2)}")
                continue
            if not path or not ln or ln[0] not in "+-":
                continue
            sign, body = ln[0], ln[1:]
            if sign == "+":
                c.lines_added += 1
            else:
                c.lines_removed += 1
            d = DEF_RE.match(body)
            if d:
                key = f"{path}::{d.group(2)}"
                (c.added_defs if sign == "+" else c.removed_defs).add(key)
                c.touched_symbols.add(key)
                continue
            k = CONST_RE.match(body.lstrip()) if len(body) - len(body.lstrip()) <= 8 else None
            if k:
                key = f"{path}::{k.group(1)}"
                c.touched_symbols.add(key)
                if sign == "+":
                    c.const_values[key] = k.group(2).strip()[:120]
                    c.const_removed.discard(key)
                elif key not in c.const_values:
                    c.const_removed.add(key)
        # a def removed and re-added in the same commit is an edit, not a lifecycle event
        both = c.added_defs & c.removed_defs
        c.added_defs -= both
        c.removed_defs -= both
        # same name removed from one file and added to another = a move
        added_names = {k.split("::")[1] for k in c.added_defs}
        removed_names = {k.split("::")[1] for k in c.removed_defs}
        moved = added_names & removed_names
        c.added_defs = {k for k in c.added_defs if k.split("::")[1] not in moved}
        c.removed_defs = {k for k in c.removed_defs if k.split("::")[1] not in moved}

    verify_lifecycle(repo, commits)


class _Blobs:
    """Fast file-at-commit reads through one `git cat-file --batch` process."""

    def __init__(self, repo: str):
        self.p = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.cache: dict[str, str | None] = {}

    def get(self, rev: str, path: str) -> str | None:
        key = f"{rev}:{path}"
        if key in self.cache:
            return self.cache[key]
        self.p.stdin.write((key + "\n").encode())
        self.p.stdin.flush()
        header = self.p.stdout.readline().decode()
        if header.endswith("missing\n"):
            self.cache[key] = None
            return None
        size = int(header.split()[2])
        data = self.p.stdout.read(size + 1)[:-1].decode("utf-8", errors="replace")
        self.cache[key] = data
        return data

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def _defines(src: str | None, name: str) -> bool:
    if not src:
        return False
    rx = re.compile(rf"^\s*(?:(?:async\s+)?(?:def|class)\s+{re.escape(name)}\b|{re.escape(name)}\s*(?::[^=\n]+)?=(?!=))", re.M)
    return bool(rx.search(src))


def verify_lifecycle(repo: str, commits: list[Commit]) -> None:
    """A diff line is a hint; the file at that commit is the truth.

    A name counts as *introduced* only if the file did not define it before the
    commit, and *removed* only if the file no longer defines it after.
    Indentation changes (moving a variable into a try: block) stop looking
    like deletions.
    """
    blobs = _Blobs(repo)
    try:
        for c in commits:
            parent = c.parents[0] if c.parents else None
            for key in list(c.removed_defs | c.const_removed):
                path, name = key.split("::", 1)
                if _defines(blobs.get(c.sha, path), name):
                    c.removed_defs.discard(key)
                    c.const_removed.discard(key)
            for key in list(c.added_defs):
                path, name = key.split("::", 1)
                if parent and _defines(blobs.get(parent, path), name):
                    c.added_defs.discard(key)
            c.lifecycle_new = {k for k in c.const_values
                               if not (parent and _defines(blobs.get(parent, k.split("::", 1)[0]), k.split("::", 1)[1]))}
    finally:
        blobs.close()
