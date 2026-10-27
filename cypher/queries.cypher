// Kyun — queries to run in Neo4j Browser / Aura console after `kyun push`.
// Each one is a question the graph answers by structure, not by text search.

// 1. The full story of a feature: every decision about SSLContext, oldest first,
//    with what overrode what and why Kyun believes it.
MATCH (c:Concept {key: 'sslcontext'})<-[:ABOUT]-(d:Decision)
OPTIONAL MATCH (d)-[s:SUPERSEDES]->(old:Decision)
RETURN d.date, d.title, d.kind, old.title AS overrides, s.rule, s.evidence
ORDER BY d.date;

// 2. Decisions that are still quoted in release notes but were later overridden.
//    These are the answers a search box gets wrong.
MATCH (newer:Decision)-[s:SUPERSEDES]->(old:Decision)-[:DOCUMENTED_IN]->(rn:ReleaseNote)
RETURN old.title, rn.version AS still_in_release_notes, newer.title AS overridden_by,
       newer.date, s.rule, s.evidence
ORDER BY newer.date DESC;

// 3. Who to ask about a file: recent decision makers on the code, and whether they are still around.
MATCH (f:File {path: 'src/requests/adapters.py'})<-[:ABOUT]-(d:Decision)-[:DECIDED_BY]->(p:Person)
WHERE NOT p.is_bot AND d.date >= '2023-01-01'
RETURN p.name, p.last_active, count(DISTINCT d) AS decisions, collect(DISTINCT d.title)[0..3] AS examples
ORDER BY decisions DESC;

// 4. Entity resolution: one engineer, many identities.
MATCH (p:Person) WHERE size(p.names) > 1 OR size(p.emails) > 1
RETURN p.name, p.names, p.emails, p.commits ORDER BY p.commits DESC LIMIT 15;

// 5. Lifecycle of one function across a file move (requests/ -> src/requests/).
MATCH (s:Symbol {name: '_preloaded_ssl_context'})-[:DEFINED_IN]->(f:File)
MATCH (d:Decision)-[a:ABOUT]->(s)
RETURN f.path, f.aliases, d.date, d.title, a.change ORDER BY d.date;

// 6. Configuration values through time, and the change that set each one.
MATCH (k:ConfigKey)<-[a:ABOUT {change: 'set'}]-(d:Decision)
RETURN k.module + '.' + k.name AS key, a.value, a.previous, d.date, d.title
ORDER BY key, d.date;

// 7. Security incidents and every decision that responded to them.
MATCH (i:Incident)<-[:RESPONDS_TO]-(d:Decision)
OPTIONAL MATCH (d)-[:DOCUMENTED_IN]->(rn:ReleaseNote)
RETURN i.ref, d.date, d.title, collect(rn.version) AS releases ORDER BY d.date;

// 8. Knowledge at risk: code whose decisions were all made by people who have gone quiet.
MATCH (s:Symbol)<-[:ABOUT]-(d:Decision)-[:DECIDED_BY]->(p:Person)
WHERE NOT s.is_test AND NOT p.is_bot
WITH s, collect(DISTINCT p) AS people, count(DISTINCT d) AS decisions
WHERE decisions >= 5 AND all(x IN people WHERE x.last_active < '2024-01-01')
RETURN s.module + '.' + s.name AS symbol, decisions, [x IN people | x.name][0..3] AS only_known_by
ORDER BY decisions DESC LIMIT 20;
