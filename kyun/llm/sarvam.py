"""Answers in Indian languages via Sarvam's translation API (optional).

Code identifiers, PR numbers, versions, dates and names must survive
translation unchanged, so they are masked with placeholders before the text is
sent and restored afterwards. Only the prose around the evidence is
translated; the evidence itself (quotes from commits and release notes) is
kept in the original, with a translated gloss beside it.

Set SARVAM_API_KEY. Endpoint: POST https://api.sarvam.ai/translate
"""
from __future__ import annotations

import json
import os
import re
import urllib.request

URL = "https://api.sarvam.ai/translate"
LANGS = {"hi-IN": "Hindi", "te-IN": "Telugu", "ta-IN": "Tamil", "kn-IN": "Kannada", "ml-IN": "Malayalam",
         "mr-IN": "Marathi", "bn-IN": "Bengali", "gu-IN": "Gujarati", "pa-IN": "Punjabi", "od-IN": "Odia"}

PROTECT_RE = re.compile(
    r"`[^`]+`|#\d+|\b\d{4}-\d{2}-\d{2}\b|\bv?\d+(?:\.\d+)+\b|\b[0-9a-f]{8,40}\b|"
    r"\b[A-Za-z_]+\.[A-Za-z_.]+\b|\b\w*_\w+\b|\b[A-Z][a-z]+[A-Z]\w*\b|\b[A-Z]{2,}\w*\b|CVE-\d{4}-\d+")


def mask(text: str, names: list[str] = ()) -> tuple[str, dict]:
    table = {}

    def keep(m):
        k = f"[[{len(table)}]]"
        table[k] = m.group(0)
        return k

    for n in sorted(set(names), key=len, reverse=True):
        if n and n in text:
            k = f"[[{len(table)}]]"
            table[k] = n
            text = text.replace(n, k)
    return PROTECT_RE.sub(keep, text), table


def unmask(text: str, table: dict) -> str:
    for k, v in table.items():
        text = text.replace(k, v)
    return text


def translate(text: str, target: str, key: str | None = None, opener=urllib.request.urlopen) -> str:
    key = key or os.environ.get("SARVAM_API_KEY")
    if not key:
        raise RuntimeError("SARVAM_API_KEY is not set")
    body = json.dumps({"input": text[:1900], "source_language_code": "en-IN", "target_language_code": target,
                       "model": "sarvam-translate:v1", "mode": "formal", "numerals_format": "international"}).encode()
    req = urllib.request.Request(URL, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "api-subscription-key": key})
    with opener(req, timeout=30) as r:
        return json.loads(r.read().decode())["translated_text"]


def translate_answer(ans, target: str, translate_fn=translate):
    """Adds `headline_<lang>` and a translated gloss to each reason; never replaces the evidence."""
    names = [p["name"] for p in ans.ask] + [p["name"] for p in (ans.current or {}).get("people", [])]
    masked, table = mask(ans.headline, names)
    ans.translated = {"lang": target, "language": LANGS.get(target, target),
                      "headline": unmask(translate_fn(masked, target), table), "why": []}
    for w in ans.why:
        m, t = mask(w["text"], names)
        ans.translated["why"].append({"original": w["text"], "gloss": unmask(translate_fn(m, target), t)})
    return ans
