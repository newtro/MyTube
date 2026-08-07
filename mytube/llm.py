"""OpenRouter chat-completions client (query bootstrap now; NL steering in
Phase 4). Same key as embeddings; model configurable."""

from __future__ import annotations

import json
import math

import httpx

from mytube.config import Settings


class LLMError(Exception):
    pass


def chat(settings: Settings, messages: list[dict], *, max_tokens: int = 800) -> str:
    if not settings.openrouter_api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")
    response = httpx.post(
        f"{settings.openrouter_base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json={
            "model": settings.llm_model,
            "messages": messages,
            "max_tokens": max_tokens,
        },
        timeout=60.0,
    )
    if response.status_code != 200:
        raise LLMError(f"HTTP {response.status_code}: {response.text[:300]}")
    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError(f"malformed completion response: {exc}") from exc
    if not isinstance(content, str) or not content.strip():
        raise LLMError("empty completion")
    return content


def generate_steering(settings: Settings, text: str) -> dict:
    """Parse a natural-language steering request (DEC-19) into signed
    pseudo-taste terms and query seeds. Strictly validated; raises LLMError
    on ANY deviation so nothing partial is ever stored (AC-13)."""
    content = chat(
        settings,
        [
            {
                "role": "system",
                "content": (
                    "You translate steering requests for a personal YouTube "
                    "recommender into JSON. Reply with ONE JSON object only — "
                    "no prose, no code fences — shaped exactly:\n"
                    '{"summary": "one-line restatement", "terms": [{"text": '
                    '"topic phrase to boost or suppress", "polarity": 1 or -1,'
                    ' "weight": 0.5-3.0}], "queries": ["youtube search query"]}'
                    "\npolarity 1 boosts, -1 suppresses. weight scales "
                    "strength (1 = normal). queries are ONLY for boosted "
                    "topics worth actively searching (0-3 of them)."
                ),
            },
            {"role": "user", "content": text.strip()[:1000]},
        ],
    )
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model did not return JSON: {raw[:200]}") from exc
    if not isinstance(parsed, dict):
        raise LLMError("model did not return a JSON object")
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LLMError("missing summary")
    terms_raw = parsed.get("terms")
    if not isinstance(terms_raw, list) or not terms_raw:
        raise LLMError("missing terms")
    if len(terms_raw) > settings.steering_max_terms:
        raise LLMError(
            f"too many terms ({len(terms_raw)} > {settings.steering_max_terms})"
        )
    terms = []
    for entry in terms_raw:
        if not isinstance(entry, dict):
            raise LLMError("term is not an object")
        t = entry.get("text")
        polarity = entry.get("polarity")
        weight = entry.get("weight", 1.0)
        if not isinstance(t, str) or not t.strip():
            raise LLMError("term text missing")
        # exact int 1/-1: equality membership would admit JSON true / 1.0
        if (
            isinstance(polarity, bool)
            or not isinstance(polarity, int)
            or polarity not in (1, -1)
        ):
            raise LLMError(f"bad polarity {polarity!r}")
        if (
            not isinstance(weight, int | float)
            or isinstance(weight, bool)
            or not math.isfinite(weight)      # json.loads admits NaN/Infinity
        ):
            raise LLMError(f"bad weight {weight!r}")
        terms.append({
            "text": " ".join(t.split())[:200],
            "polarity": polarity,
            "weight": float(min(max(weight, 0.25), 3.0)),
        })
    queries_raw = parsed.get("queries", [])
    if not isinstance(queries_raw, list):
        raise LLMError("queries is not a list")
    queries: list[str] = []
    for entry in queries_raw:
        # strict like everything else (AC-13): a malformed entry fails the
        # whole parse rather than silently applying a partial directive
        if not isinstance(entry, str) or not entry.strip():
            raise LLMError(f"bad query entry {entry!r}")
        q = " ".join(entry.split())[:120]
        if q.lower() not in {x.lower() for x in queries}:
            queries.append(q)
    return {
        "summary": summary.strip()[:300],
        "terms": terms,
        "queries": queries[: settings.steering_max_queries],
    }


def generate_queries(
    settings: Settings, taste_snippets: list[str], count: int
) -> list[str]:
    """Taste-cluster-derived search queries (AC-32 bootstrap): the model
    sees a sample of liked/watched titles and proposes distinct queries.
    Returns a deduped list; raises LLMError on any failure (no partial
    application)."""
    sample = "\n".join(f"- {s}" for s in taste_snippets[:60])
    content = chat(
        settings,
        [
            {
                "role": "system",
                "content": (
                    "You generate YouTube search queries for a personal "
                    "recommender. Reply with a JSON array of strings only — "
                    "no prose, no code fences."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"From these titles a user liked or watched, derive "
                    f"{count} diverse search queries covering the distinct "
                    f"interest clusters (2-6 words each, no hashtags):\n{sample}"
                ),
            },
        ],
    )
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"model did not return a JSON array: {text[:200]}") from exc
    if not isinstance(parsed, list):
        raise LLMError("model did not return a JSON array")
    queries: list[str] = []
    for entry in parsed:
        if isinstance(entry, str):
            q = " ".join(entry.split())[:120]
            if q and q.lower() not in {x.lower() for x in queries}:
                queries.append(q)
    if not queries:
        raise LLMError("model returned no usable queries")
    return queries[:count]
