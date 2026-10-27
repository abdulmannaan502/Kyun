"""Optional: pull request and issue discussions from the GitHub REST API.

Git history and the in-repo changelog are enough for Kyun to work. With a
GITHUB_TOKEN this module adds the discussion threads behind each PR/issue
number found in history, which become Discussion nodes in the graph.

Results are cached on disk, so a repository is fetched once.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

API = "https://api.github.com"


@dataclass
class Thread:
    number: int
    kind: str                 # "pull" or "issue"
    title: str
    body: str
    author: str
    created: str
    closed: str | None
    labels: list[str] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)   # {author, created, body}
    url: str = ""


class GitHubClient:
    def __init__(self, owner: str, repo: str, cache_dir: str, token: str | None = None,
                 opener=urllib.request.urlopen):
        self.owner, self.repo = owner, repo
        self.cache_dir = os.path.join(cache_dir, f"{owner}__{repo}")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self._open = opener

    def _get(self, path: str):
        req = urllib.request.Request(API + path, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "kyun-why-engine",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        for attempt in range(3):
            try:
                with self._open(req, timeout=20) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code in (403, 429) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise
        return None

    def thread(self, number: int) -> Thread | None:
        cache = os.path.join(self.cache_dir, f"{number}.json")
        if os.path.exists(cache):
            with open(cache, encoding="utf-8") as fh:
                d = json.load(fh)
            return Thread(**d) if d else None
        base = f"/repos/{self.owner}/{self.repo}/issues/{number}"
        issue = self._get(base)
        if not issue:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(None, fh)
            return None
        comments = self._get(base + "/comments?per_page=100") or []
        t = Thread(
            number=number,
            kind="pull" if "pull_request" in issue else "issue",
            title=issue.get("title") or "",
            body=(issue.get("body") or "")[:8000],
            author=(issue.get("user") or {}).get("login", ""),
            created=issue.get("created_at") or "",
            closed=issue.get("closed_at"),
            labels=[lb.get("name", "") for lb in issue.get("labels", [])],
            comments=[{"author": (c.get("user") or {}).get("login", ""),
                       "created": c.get("created_at", ""),
                       "body": (c.get("body") or "")[:4000]} for c in comments],
            url=issue.get("html_url") or "",
        )
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(t.__dict__, fh)
        return t


def load_cached_threads(cache_dir: str, owner: str, repo: str) -> dict[int, Thread]:
    """Read whatever has been fetched before, without touching the network."""
    d = os.path.join(cache_dir, f"{owner}__{repo}")
    out: dict[int, Thread] = {}
    if not os.path.isdir(d):
        return out
    for name in os.listdir(d):
        if name.endswith(".json"):
            with open(os.path.join(d, name), encoding="utf-8") as fh:
                data = json.load(fh)
            if data:
                out[data["number"]] = Thread(**data)
    return out
