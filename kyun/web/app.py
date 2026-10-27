"""Web UI: ask a question, see the answer, its evidence chain and the graph behind it."""
from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

from ..graph.store import MemoryGraph
from ..insights import knowledge_at_risk, overview, stale_release_notes
from ..qa.baseline import FlatSearch
from ..qa.engine import WhyEngine

HERE = os.path.dirname(__file__)

EXAMPLES = {
    "psf/requests": [
        "Why doesn't requests cache a default SSLContext?",
        "Why is the retry limit three?",
        "Why does requests use charset_normalizer instead of chardet?",
        "Why is get_connection deprecated?",
        "Who should I ask about adapters.py?",
        "Why does requests not support HTTP/2?",
    ],
    "pallets/flask": [
        "Why does redirect return 303 instead of 302?",
        "Why is before_first_request deprecated?",
        "Why is the URL matched after the session is loaded?",
        "Who should I ask about app.py?",
        "Why does Flask use Kubernetes for deployment?",
    ],
}


def create_app(graph_path: str) -> Flask:
    g = MemoryGraph.load(graph_path)
    engine = WhyEngine(g)
    flat = FlatSearch(g)
    ov = overview(g)
    app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")

    @app.get("/")
    def index():
        return send_from_directory(os.path.join(HERE, "static"), "index.html")

    @app.get("/api/ask")
    def ask():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify({"error": "Type a question about the code, for example: why is X like this?"}), 400
        ans = engine.ask(q).to_dict()
        lang = request.args.get("lang")
        if lang:
            if not os.environ.get("SARVAM_API_KEY"):
                ans["translated_error"] = "Set SARVAM_API_KEY on the server to get answers in Indian languages."
            else:
                from ..llm.sarvam import translate_answer
                a = engine.ask(q)
                translate_answer(a, lang)
                ans = a.to_dict()
        ans["baseline"] = flat.search(q, k=3)
        return jsonify(ans)

    @app.get("/api/overview")
    def api_overview():
        return jsonify({**ov, "examples": EXAMPLES.get(g.meta.get("repo"), [])})

    @app.get("/api/risk")
    def api_risk():
        return jsonify({"at_risk": knowledge_at_risk(g), "stale_notes": stale_release_notes(g, 12)})

    return app


if __name__ == "__main__":
    import sys
    create_app(sys.argv[1]).run(port=int(os.environ.get("PORT", 8000)))
