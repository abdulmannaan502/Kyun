(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  // `code` spans in commit text become real code
  const rich = (s) => esc(s).replace(/``?([^`]+)``?/g, "<code>$1</code>");
  let repo = "";
  let cy = null;

  const STATUS = {
    answered: "Answer",
    what_not_why: "No reason recorded",
    not_in_graph: "Not in the history",
  };

  async function boot() {
    const ov = await (await fetch("/api/overview")).json();
    repo = ov.meta.repo;
    const st = ov.stats;
    $("repo").innerHTML = `Reading <b>${esc(repo)}</b>: ${ov.meta.commits.toLocaleString()} commits from ${ov.meta.first_commit.slice(0, 4)} to ${ov.meta.last_commit.slice(0, 4)}, ` +
      `${st.nodes.Decision.toLocaleString()} decisions, ${st.total_relationships.toLocaleString()} relationships.`;
    const ex = $("examples");
    for (const q of ov.examples) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = q;
      b.onclick = () => { $("q").value = q; run(q); };
      ex.appendChild(b);
    }
    const params = new URLSearchParams(location.search);
    if (params.get("q")) { $("q").value = params.get("q"); run(params.get("q")); }
    loadRisk();
  }

  $("ask-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("q").value.trim();
    if (q) run(q);
  });

  async function run(q) {
    const res = $("result");
    res.hidden = false;
    res.classList.add("loading");
    $("headline").textContent = "Walking the graph…";
    const r = await fetch("/api/ask?q=" + encodeURIComponent(q));
    const a = await r.json();
    res.classList.remove("loading");
    if (a.error) { $("headline").textContent = a.error; return; }
    history.replaceState(null, "", "?q=" + encodeURIComponent(q));
    render(a);
  }

  function render(a) {
    const st = $("status");
    st.className = "status " + a.status;
    st.innerHTML = `<span class="pill">${STATUS[a.status]}</span>` +
      `<span class="meter" title="Confidence"><span class="bar"><i style="width:${Math.round(a.confidence * 100)}%"></i></span>${a.confidence.toFixed(2)} confidence</span>`;

    let head = a.headline;
    const prem = head.match(/^Check the premise: (.+?, not [^\s.]+\.)\s*/);
    $("premise").innerHTML = "";
    if (prem) {
      $("premise").innerHTML = `<div class="premise"><b>Check the premise.</b> ${rich(prem[1])}</div>`;
      head = head.slice(prem[0].length);
    }
    $("headline").innerHTML = rich(head);

    // why
    $("why-block").hidden = !a.why.length;
    $("why").innerHTML = a.why.map((w) =>
      `<blockquote>${rich(w.text)}<cite>${esc(w.source)}</cite></blockquote>`).join("");

    // timeline
    const chain = a.chain || [];
    $("chain-block").hidden = !chain.length;
    $("chain").innerHTML = chain.map((c) => {
      if (c.edge) {
        return `<li class="edge"><b>Overridden.</b> ${rich(c.evidence)} <span class="mono">[${esc(c.rule)}]</span></li>`;
      }
      const status = c.status === "live" ? "live" : c.status === "superseded" ? "superseded" : "";
      const tag = status === "live" ? "current" : status === "superseded" ? "overridden" :
        (c.status && c.status !== "related" ? esc(c.status) : "");
      const pr = c.pr ? `<a href="https://github.com/${esc(repo)}/pull/${c.pr}" target="_blank" rel="noopener">#${c.pr}</a> ` : "";
      const val = c.value ? ` set to <code>${esc(c.value)}</code>` : "";
      return `<li class="dec ${status}"><span class="when">${esc(c.date)}</span>${tag ? `<span class="tag">${tag}</span>` : ""}
        <span class="title">${rich(c.title)}</span>
        <span class="by">${pr}${(c.people || []).map(esc).join(", ")}${val}</span></li>`;
    }).join("");

    // people
    $("ask-block").hidden = !a.ask.length;
    $("people").innerHTML = a.ask.map((p) => {
      const aka = (p.aliases || []).filter((n) => n !== p.name);
      return `<li><span class="name">${esc(p.name)}</span>
        <span class="state ${p.active ? "" : "gone"}">${p.active ? "active, last commit " + esc(p.last_active) : "inactive since " + esc(p.last_active)}</span>
        ${aka.length ? `<span class="aka">also commits as ${aka.map(esc).join(", ")}</span>` : ""}</li>`;
    }).join("");

    // confidence
    const parts = Object.entries(a.confidence_parts || {});
    $("conf-summary").textContent = `How sure is this? ${a.confidence_label} (${a.confidence.toFixed(2)})`;
    $("conf-parts").innerHTML = parts.length
      ? parts.map(([k, v]) => `<div class="conf-row"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("")
      : `<div class="conf-row"><span>No decision matched</span><span>Kyun refuses rather than guess.</span></div>`;

    renderBaseline(a);
    $("cypher").textContent = a.cypher || "";
    renderGraph(a.subgraph || { nodes: [], edges: [] });
  }

  function renderBaseline(a) {
    const old = new Set();
    for (const s of a.superseded || []) {
      (s.shas || []).forEach((x) => old.add(x));
      (s.notes || []).forEach((n) => old.add(n.id));
    }
    const hits = a.baseline || [];
    $("baseline").innerHTML = hits.length ? hits.map((h, i) => {
      const key = h.id.startsWith("commit:") ? h.id.slice(7, 15) : h.id;
      const stale = old.has(key);
      return `<div class="hit ${stale ? "stale" : ""}"><span class="src">${i === 0 ? "top result, " : ""}${esc(h.id.startsWith("rn:") ? "release notes " + h.id.split(":")[1] : "commit " + key)}, ${esc(h.date)}</span>
        <div>${rich(h.text.slice(0, 220))}${h.text.length > 220 ? "…" : ""}</div>
        ${stale ? `<span class="warn">This describes a decision that was later overridden.</span>` : ""}</div>`;
    }).join("") : `<div class="hit">No text matches.</div>`;
    if (a.status === "not_in_graph" && hits.length) {
      $("baseline").insertAdjacentHTML("afterbegin", `<p class="hint">Search still returns something. Kyun refuses because none of it is about the question.</p>`);
    }
  }

  function renderGraph(sg) {
    const colors = { Decision: "#4A5A6E", Symbol: "#2F5D8A", ConfigKey: "#2F5D8A", File: "#2F5D8A", Concept: "#2F5D8A",
      ReleaseNote: "#A9741B", Person: "#18263A", PullRequest: "#7B8898", Commit: "#7B8898", Incident: "#9B2C2C", Issue: "#7B8898" };
    const live = new Set(), old = new Set();
    sg.edges.forEach((e) => { if (e.type === "SUPERSEDES") { live.add(e.source); old.add(e.target); } });
    const els = [];
    sg.nodes.forEach((n) => {
      let c = colors[n.label] || "#7B8898";
      if (n.label === "Decision") c = old.has(n.id) ? "#7E4A78" : "#0E7466";
      els.push({ data: { id: n.id, label: n.name, color: c, kind: n.label, seed: n.seed ? 1 : 0 } });
    });
    sg.edges.forEach((e, i) => els.push({ data: { id: "e" + i, source: e.source, target: e.target, label: e.type === "SUPERSEDES" ? "overrides" : e.type.toLowerCase().replace("_", " "), sup: e.type === "SUPERSEDES" ? 1 : 0 } }));
    if (cy) cy.destroy();
    if (!window.cytoscape || !els.length) { $("graph").innerHTML = ""; return; }
    cy = cytoscape({
      container: $("graph"), elements: els, wheelSensitivity: 0.2, maxZoom: 1.6, minZoom: 0.3,
      style: [
        { selector: "node", style: { "background-color": "data(color)", label: "data(label)", "font-family": "Instrument Sans, sans-serif", "font-size": 9, color: "#18263A", "text-wrap": "wrap", "text-max-width": 110, "text-valign": "bottom", "text-margin-y": 4, width: 16, height: 16 } },
        { selector: 'node[kind = "Decision"]', style: { width: 24, height: 24, "font-size": 10, "font-weight": 600 } },
        { selector: 'node[kind = "Symbol"], node[kind = "ConfigKey"], node[kind = "File"], node[kind = "Concept"], node[kind = "ReleaseNote"]', style: { shape: "round-rectangle" } },
        { selector: "node[seed = 1]", style: { "border-width": 3, "border-color": "#18263A" } },
        { selector: "edge", style: { width: 1.2, "line-color": "#B9C3CD", "target-arrow-color": "#B9C3CD", "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": .7, label: "data(label)", "font-size": 7, color: "#7B8898", "text-rotation": "autorotate", "text-background-color": "#EEF1F3", "text-background-opacity": 1, "text-background-padding": 1 } },
        { selector: "edge[sup = 1]", style: { width: 2.5, "line-color": "#7E4A78", "target-arrow-color": "#7E4A78", color: "#7E4A78", "font-size": 9, "font-weight": 600 } },
      ],
      layout: { name: "cose", animate: false, nodeRepulsion: 9000, idealEdgeLength: 70, padding: 18 },
    });
  }

  async function loadRisk() {
    const r = await (await fetch("/api/risk")).json();
    $("risk-table").querySelector("tbody").innerHTML = r.at_risk.slice(0, 10).map((x) =>
      `<tr><td><code>${esc(x.symbol)}</code></td><td class="n">${x.decisions}</td><td>${x.known_by.map(esc).join(", ")}</td><td class="n">${esc(x.last_seen)}</td></tr>`).join("");
    $("stale").innerHTML = r.stale_notes.slice(0, 6).map((x) =>
      `<li><span class="v">release notes ${esc(x.version)}</span> ${rich(x.note)}<br><span class="by">Overridden on ${esc(x.on)} by “${rich(x.overridden_by)}”, ${esc(x.rule)}.</span></li>`).join("");
  }

  boot();
})();
