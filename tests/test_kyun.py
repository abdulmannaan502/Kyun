"""End-to-end and unit tests. Run: python -m pytest -q"""
import io
import json
import os
import subprocess
import textwrap

import pytest

from kyun.extract import text as tx
from kyun.graph.build import build_graph
from kyun.graph.neo4j_store import _flat, _unflat, export_csv, write_batches
from kyun.graph.store import MemoryGraph
from kyun.ingest.changelog import parse_changelog
from kyun.ingest.github_api import GitHubClient
from kyun.llm.sarvam import mask, translate_answer, unmask
from kyun.qa.baseline import FlatSearch
from kyun.qa.engine import WhyEngine, stem
from kyun.resolve.entities import PeopleResolver, canonical_paths


# ---------------------------------------------------------------- a tiny repository with a real story
def _git(repo, *args, env=None):
    e = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", **(env or {}))
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=e)


def _commit(repo, msg, name, email, date, files):
    for path, content in files.items():
        full = os.path.join(repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if content is None:
            os.remove(full)
        else:
            with open(full, "w") as fh:
                fh.write(textwrap.dedent(content))
    _git(repo, "add", "-A")
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_AUTHOR_DATE": date,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email, "GIT_COMMITTER_DATE": date}
    _git(repo, "commit", "-q", "-m", msg, env=env)


BASE = """\
DEFAULT_TIMEOUT = 30
MAX_RETRIES = 0


def fetch(url):
    return url
"""

CACHED = BASE + """

_shared_context = object()


def get_context():
    return _shared_context
"""

CHANGELOG_1 = """\
1.1.0 (2024-03-01)
------------------

- `fetch` now reuses a shared context to improve performance. (#12)

1.0.0 (2024-01-01)
------------------

- First release.
"""

CHANGELOG_2 = """\
1.2.0 (2024-09-01)
------------------

- The shared context feature originally introduced in 1.1.0 caused
  thread-safety bugs, so the team decided to revert it. (#15)

""" + CHANGELOG_1


@pytest.fixture(scope="module")
def tiny(tmp_path_factory):
    repo = str(tmp_path_factory.mktemp("tiny"))
    _git(repo, "init", "-q", "-b", "main")
    _commit(repo, "Initial client", "Asha Rao", "asha@work.example", "2024-01-01T10:00:00+05:30",
            {"lib/client.py": BASE, "HISTORY.md": "1.0.0 (2024-01-01)\n------------------\n\n- First release.\n"})
    _commit(repo, "Reuse a shared context to improve performance (#12)", "Vikram S", "vik@example.org",
            "2024-02-20T10:00:00+05:30", {"lib/client.py": CACHED, "HISTORY.md": CHANGELOG_1})
    _commit(repo, "Move client into src (#13)", "Asha R. Rao", "asha@work.example", "2024-04-01T10:00:00+05:30",
            {"lib/client.py": None, "src/client.py": CACHED})
    _commit(repo, "Raise the default timeout", "Asha Rao", "asha.rao@home.example", "2024-05-01T10:00:00+05:30",
            {"src/client.py": CACHED.replace("DEFAULT_TIMEOUT = 30", "DEFAULT_TIMEOUT = 60")})
    _commit(repo, "Revert shared context caching (#15)", "Asha Rao", "asha@work.example", "2024-08-20T10:00:00+05:30",
            {"src/client.py": BASE.replace("DEFAULT_TIMEOUT = 30", "DEFAULT_TIMEOUT = 60"), "HISTORY.md": CHANGELOG_2})
    g = build_graph(repo, "acme/tiny", log=lambda *a: None)
    return g


def test_file_rename_is_one_entity(tiny):
    f = tiny.node("file:src/client.py")
    assert "lib/client.py" in f["aliases"]
    assert "file:lib/client.py" not in tiny.nodes


def test_symbol_survives_the_move(tiny):
    sym = "sym:src/client.py::_shared_context"
    changes = sorted(p.get("change") for d, _, p in tiny.inc(sym, "ABOUT"))
    assert "introduce" in changes and "remove" in changes


def test_people_merged_by_email(tiny):
    asha = [p for p in tiny.by_label("Person") if "Asha Rao" in tiny.node(p)["names"]]
    assert len(asha) == 1
    assert "Asha R. Rao" in tiny.node(asha[0])["names"]


def test_supersession_detected(tiny):
    edges = [(s, d, p["rule"]) for s, r, d, p in tiny.edges() if r == "SUPERSEDES"]
    newer = {s for s, _, _ in edges}
    assert any("15" in s for s in newer)
    rules = {r for *_, r in edges}
    assert rules & {"code lifecycle", "changelog statement"}


def test_config_value_history(tiny):
    key = "cfg:src/client.py::DEFAULT_TIMEOUT"
    vals = sorted(p.get("value") for _, _, p in tiny.inc(key, "ABOUT") if p.get("change") == "set")
    assert vals == ["30", "60"]


def test_answer_names_live_and_overridden(tiny):
    a = WhyEngine(tiny).ask("Why doesn't the client reuse a shared context?")
    assert a.status == "answered"
    assert "Revert" in a.current["title"]
    assert a.superseded and "shared context" in a.superseded[0]["title"].lower()
    assert any("thread-safety" in w["text"] for w in a.why)


def test_refuses_unknown(tiny):
    a = WhyEngine(tiny).ask("Why was the GraphQL layer removed?")
    assert a.status == "not_in_graph" and a.confidence == 0


def test_premise_check(tiny):
    a = WhyEngine(tiny).ask("Why is MAX_RETRIES three?")
    assert a.headline.startswith("Check the premise")


def test_baseline_runs(tiny):
    assert FlatSearch(tiny).search("shared context")


def test_save_load_roundtrip(tiny, tmp_path):
    p = str(tmp_path / "g.json.gz")
    tiny.save(p)
    g2 = MemoryGraph.load(p)
    assert g2.stats() == tiny.stats()


# ---------------------------------------------------------------- neo4j export
def test_neo4j_batches_are_primitive(tiny):
    prim = (str, int, float, bool)
    n = 0
    for cypher, params in write_batches(tiny):
        assert "MERGE" in cypher
        rows = params.get("rows") or [params["meta"]]
        for row in rows:
            props = row.get("p", row)
            for v in props.values():
                assert isinstance(v, prim) or (isinstance(v, list) and all(isinstance(x, prim) for x in v))
        n += 1
    assert n > 5


def test_flat_roundtrip():
    p = {"a": 1, "rationale": [{"text": "x", "source": "y"}], "tags": ["a", "b"]}
    assert _unflat(_flat(p)) == p


def test_export_csv(tiny, tmp_path):
    files = export_csv(tiny, str(tmp_path))
    assert any(f.endswith("load.cypher") for f in files)
    assert os.path.exists(tmp_path / "nodes_Decision.csv")


# ---------------------------------------------------------------- units
def test_ident_keys_join_vocabularies():
    assert "sslcontext" in tx.ident_keys("_preloaded_ssl_context")
    assert "sslcontext" in tx.ident_keys("SSLContext")


def test_stem_consistent():
    assert stem("cookies") == stem("cookie")
    assert stem("retries") == stem("retry")
    assert stem("dropped") == stem("drop")
    assert stem("caching") == stem("cache")


def test_rationale_spans():
    s = tx.rationale_spans("Switch to X. We do this because the license is ambiguous.")
    assert s == ["We do this because the license is ambiguous."]


def test_changelog_formats(tmp_path):
    md = tmp_path / "HISTORY.md"
    md.write_text("2.0.0 (2024-01-02)\n------------------\n\n**Bugfixes**\n\n- Fixed a thing. (#42)\n")
    n = parse_changelog(str(md))
    assert n[0].version == "2.0.0" and n[0].refs == [42] and n[0].section == "Bugfixes"
    rst = tmp_path / "CHANGES.rst"
    rst.write_text("Version 3.1.0\n-------------\n\nReleased 2024-05-06\n\n-   Use ``x``. :pr:`77`\n")
    n = parse_changelog(str(rst))
    assert n[0].version == "3.1.0" and n[0].date == "2024-05-06" and n[0].refs == [77]


def test_canonical_paths_chain():
    m = canonical_paths([("a.py", "b.py"), ("b.py", "src/c.py")])
    assert m["a.py"] == "src/c.py"


def test_people_resolver_middle_name():
    r = PeopleResolver()
    r.add("Ian Stapleton Cordasco", "x@a.com")
    r.add("Ian Cordasco", "y@b.com")
    r.finalize()
    assert r.person_id("Ian Cordasco", "y@b.com") == r.person_id("Ian Stapleton Cordasco", "x@a.com")


def test_sarvam_masking_keeps_code():
    m, t = mask("Revert `_get_connection` in #6767 on 2025-06-13 (CVE-2024-35195)", ["Nate Prewitt"])
    assert "_get_connection" not in m and "#6767" not in m
    assert unmask(m, t).startswith("Revert `_get_connection`")


def test_translate_answer_with_fake(tiny):
    a = WhyEngine(tiny).ask("Why doesn't the client reuse a shared context?")
    translate_answer(a, "hi-IN", translate_fn=lambda text, lang: "[hi] " + text)
    assert a.translated["headline"].startswith("[hi] ")
    assert a.why and a.translated["why"][0]["original"] == a.why[0]["text"]


def test_github_client_with_fake(tmp_path):
    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass
    def fake(req, timeout=20):
        if req.full_url.endswith("/comments?per_page=100"):
            return R(json.dumps([{"user": {"login": "maint"}, "created_at": "x", "body": "We revert because it breaks threads."}]).encode())
        return R(json.dumps({"title": "Revert", "body": "b", "user": {"login": "a"}, "created_at": "t",
                             "closed_at": None, "labels": [], "pull_request": {}, "html_url": "u"}).encode())
    c = GitHubClient("o", "r", str(tmp_path), token="t", opener=fake)
    t = c.thread(5)
    assert t.kind == "pull" and t.comments[0]["author"] == "maint"
    assert c.thread(5).title == "Revert"     # cached
