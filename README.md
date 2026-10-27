# Kyun — ask a codebase *why* it is the way it is

**hackFront India 2026 · Theme T1 AI and Developer Tools · Problem statement T1.1 "Why The Code Is Like This"**
Tracks: Neo4j (graph is the system of record) · Sarvam (answers in Indian languages, optional)

> *Kyun* (क्यों) is Hindi for "why".

A new engineer can read code. They cannot read intent. Kyun reads a repository's history —
commits, pull requests, release notes, and (optionally) GitHub discussions — and builds a
**graph of decisions moving through time**. Ask it a question in plain English and it answers
with a chain of evidence: the decision, where it was discussed, the release note that explains it,
the date, and the person to ask. When a later decision quietly overrode an earlier one, it says so.
When the history doesn't hold a reason, it says that too, instead of inventing one.

```
$ kyun ask data/requests.json.gz "Why doesn't requests cache a default SSLContext?"

ANSWER  ·  confidence 0.93 (high)
  Revert caching a default SSLContext — decided 2025-06-13 in #6767 by Nate Prewitt. This overrides
  an earlier decision ("Avoid reloading root certificates to improve concurrent performance",
  2024-05-15), which you may still find quoted in older docs.

Why (as recorded):
  • "The SSLContext caching feature originally introduced in 2.32.0 has created a new class of
    issues in Requests that have had negative impact across a number of use cases." — release notes 2.32.5
  • "The Requests team has decided to revert this feature as long term maintenance of it is
    proving to be unsustainable in its current iteration." — release notes 2.32.5

Overridden earlier — do not rely on:
  ✗ 2024-05-15  Avoid reloading root certificates to improve concurrent performance
      → overridden 2025-06-13 (changelog statement: release notes 2.32.5: originally introduced in 2.32.0 and reverted)

Person to ask:
  → Nate Prewitt  (active)
```

The 2.32.0 release notes still say requests "reuses a global SSLContext". A search box returns that
sentence. Kyun knows it is dead.

## Run it (2 minutes, no internet needed)

Needs Python 3.10+ and git.

```bash
pip install -r requirements.txt
python -m kyun.cli serve data/requests.json.gz        # open http://127.0.0.1:8000
python -m kyun.cli ask data/requests.json.gz "Why is the retry limit three?"
python -m kyun.cli eval data/requests.json.gz eval/questions_requests.json
python -m kyun.cli risk data/requests.json.gz
python -m pytest -q
```

Windows: double-click `run_demo.bat`. macOS/Linux: `./run_demo.sh`.

Prebuilt graphs for **psf/requests** (6,496 commits, 2011–2026) and **pallets/flask** (5,562 commits)
are in `data/`. To build your own from any git repository:

```bash
git clone https://github.com/psf/requests
python -m kyun.cli build requests --name psf/requests --out data/requests.json.gz   # ~6 seconds
```

## How it works

```
git log -M (commits, renames)  ─┐
git log -p diff=python          ├─► entity resolution ─► decision graph ─► supersession ─► WhyEngine ─► answer
changelog (+ git blame)         │   files across renames                                          + evidence chain
GitHub PR/issue threads (opt.) ─┘   people across emails/names                                    + person to ask
                                    functions across moves                                        + confidence / refusal
```

**Graph model** (`cypher/schema` is created by `kyun push`): `Decision`, `Commit`, `PullRequest`,
`ReleaseNote`, `Release`, `Person`, `File`, `Symbol`, `ConfigKey`, `Concept`, `Incident`, `Issue`,
`Discussion`, joined by `ABOUT`, `SUPERSEDES`, `DECIDED_BY`, `DOCUMENTED_IN`, `RESPONDS_TO`, `MADE_IN`,
`PART_OF`, `TOUCHES`, `SETS`, `DEFINED_IN`, `AUTHORED`, `REFERENCES`, `IN_RELEASE`, `DISCUSSED_IN`.

**The hard parts, as the brief predicted:**

* **Entity resolution.** `requests/adapters.py` and `src/requests/adapters.py` are one file (rename chains
  from `git log -M`). "Ian Cordasco", "Ian Stapleton Cordasco" and `sigmavirus24@users.noreply.github.com`
  are one person (union-find on e-mail and normalised name). A question about "SSLContext" reaches code
  named `_preloaded_ssl_context` and a release note that says "SSL context" (identifier sub-keys join the
  vocabularies). Every add/remove of a function is verified against the file contents at that commit,
  so moving a variable into a `try:` block is not mistaken for deleting it.
* **Temporal reasoning.** Six explainable rules create `SUPERSEDES` edges, each stored with its evidence:
  explicit `git revert`; `Revert "<subject>"`; code lifecycle (a later change deletes what an earlier one
  introduced); changelog statements ("originally introduced in 2.32.0 … decided to revert"); config value
  changes; successive versions ("upper limit to 7" → "to 8"). The answer is always the *live* decision.
* **Honesty.** Three outcomes, never a fourth: *answered* (a reason is recorded), *no reason recorded*
  (we know what/when/who, not why — and who to ask), *not in the history* (refuse). Technical terms in a
  question that match nothing in the graph (e.g. "HTTP/2") force a refusal. A number in the question is
  checked against the code: "Why is the retry limit three?" → *Check the premise: DEFAULT_RETRIES is 0.*
* **Confidence** is built from visible parts: entity-link strength, whether a reason is recorded,
  corroboration across sources, margin over the next-best answer, and how much the code changed since.

No language model writes the answers. Every sentence is assembled from linked evidence. (An LLM can be
added for phrasing; it would only rephrase evidence it is given.)

## Evidence that the graph, not the text, does the work

`kyun eval` compares Kyun with **flat BM25 search over exactly the same commits and release notes**
(what a search box or vector-RAG over the history gives you).

| Set | Kyun | Flat search | Notes |
|---|---|---|---|
| psf/requests, 21 questions (development set) | **100%** | 26% | 3/3 unanswerable refused (search answered all 3); 4/4 overridden decisions flagged (search quoted the dead one in 1/4) |
| pallets/flask, 14 questions (**held-out**) | **78.6%** | 38.5% | written before running Kyun on Flask; no tuning after. 2/2 refusals correct |

The requests set was used during development, so treat 100% as an upper bound. The Flask set is the
honest number. Full tables: `eval/results_*.md`. Held-out failures (H06, H09, H14) are analysed in the deck:
two come from a single sweeping "remove deprecated code" commit that touches many symbols and outranks
the specific change; the fix (dilute `ABOUT` weight by the number of symbols a change touches, as already
done for files) is the first item on the roadmap. One gold label (H08) was wrong and was corrected after
scoring; this is recorded in the question file.

## Neo4j / AuraDB

The graph is designed for Neo4j end to end (`kyun/graph/neo4j_store.py`):

```bash
pip install neo4j
export NEO4J_URI=neo4j+s://<id>.databases.neo4j.io NEO4J_USERNAME=neo4j NEO4J_PASSWORD=...
python -m kyun.cli push data/requests.json.gz      # constraints, indexes, batched MERGE
python -m kyun.cli pull --out from_aura.json.gz     # the engine runs on what's in Aura
```

Then open `cypher/queries.cypher` in the Aura console: the full SSLContext story, release notes that are now
wrong, who to ask about a file, one engineer's many identities, function lifecycle across a file move,
config values through time, incidents, and *knowledge at risk* (code whose decision-makers have all left).
Offline alternative: `kyun export-csv data/requests.json.gz out/` writes CSVs plus `load.cypher`.

The Neo4j writer is unit-tested offline (every batch is checked for Neo4j-compatible property types);
it was not run against a live AuraDB from the build machine, which had no route to Neo4j's servers.

## Indian languages (Sarvam)

`kyun ask ... --lang hi-IN` (or te-IN, ta-IN, kn-IN, ml-IN, mr-IN, bn-IN, gu-IN, pa-IN, od-IN) translates the
answer with Sarvam's translate API. Code identifiers, PR numbers, versions, dates and names are masked
before translation and restored after, and the original evidence quotes are always kept next to the
translated gloss. Needs `SARVAM_API_KEY`; tested offline with a stub.

## GitHub discussions (optional)

`GITHUB_TOKEN=... python -m kyun.cli fetch-github data/requests.json.gz` caches PR/issue threads for every
number in the history; rebuild with `--github-cache .kyun/github` and PR descriptions and review comments
become `Discussion` nodes and rationale sources.

## Layout

```
kyun/ingest/     git history, changelog (+blame), GitHub threads
kyun/extract/    rule-based references, decision kinds, rationale, concepts
kyun/resolve/    people, files, symbols
kyun/graph/      store, builder, Neo4j writer/reader, CSV export
kyun/temporal/   supersession rules
kyun/qa/         WhyEngine (link → expand → thread → explain → refuse), flat-search baseline
kyun/llm/        Sarvam translation with code-safe masking
kyun/web/        Flask UI
cypher/          Neo4j queries
eval/            question sets, gold answers, results
tests/           22 tests incl. an end-to-end synthetic repository
```

## Disclosure

Built during the hackFront India 2026 league. AI assistance (Claude) was used for writing code and
documentation, as permitted by the rules. Third-party: Flask, Cytoscape.js (MIT), Instrument Sans and
JetBrains Mono (OFL), optional `neo4j` driver. Demo data: public history of psf/requests (Apache-2.0) and
pallets/flask (BSD-3-Clause).

MIT licence.
