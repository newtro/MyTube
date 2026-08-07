"""OpenRouter embedding client (DEC-5) with cost/latency capture for Phase 0.

OpenAI-compatible: swapping providers is a base-URL + key change (ASM-4).
Embedding input is metadata-only per DEC-13: title + channel + truncated
description + tags. Every stored row is tagged with its model id (§5).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import httpx
import numpy as _np

_MAX_ATTEMPTS = 4
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class EmbeddingError(Exception):
    pass


@dataclass
class EmbedStats:
    model_id: str
    calls: int = 0
    items: int = 0
    total_tokens: int = 0
    total_latency_s: float = 0.0
    dims: int | None = None

    def merge(self, other: EmbedStats) -> None:
        self.calls += other.calls
        self.items += other.items
        self.total_tokens += other.total_tokens
        self.total_latency_s += other.total_latency_s
        self.dims = self.dims or other.dims


@dataclass
class EmbedResult:
    vectors: list[list[float]]
    stats: EmbedStats = field(default_factory=lambda: EmbedStats(model_id=""))


def build_embedding_input(
    title: str | None,
    channel_title: str | None,
    description: str | None,
    tags: Sequence[str] | None,
    *,
    max_description_chars: int = 800,
    max_tags: int = 15,
) -> str:
    """Compose the DEC-13 metadata-only embedding input."""
    parts: list[str] = []
    if title:
        parts.append(title.strip())
    if channel_title:
        parts.append(f"Channel: {channel_title.strip()}")
    if description:
        desc = " ".join(description.split())[:max_description_chars]
        if desc:
            parts.append(desc)
    if tags:
        tag_str = ", ".join(t.strip() for t in list(tags)[:max_tags] if t.strip())
        if tag_str:
            parts.append(f"Tags: {tag_str}")
    return "\n".join(parts)


class OpenRouterEmbedder:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set — see docs/runbooks/phase0-setup.md")
        self._base_url = base_url.rstrip("/")
        self._sleep = sleep
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/local/mytube",
                "X-Title": "MyTube",
            },
        )

    def embed_batch(self, model_id: str, texts: Sequence[str]) -> EmbedResult:
        """Embed one batch of texts, with retry/backoff on 429/5xx."""
        if not texts:
            return EmbedResult(vectors=[], stats=EmbedStats(model_id=model_id))
        payload = {"model": model_id, "input": list(texts)}
        last_error = ""
        for attempt in range(_MAX_ATTEMPTS):
            started = time.monotonic()
            try:
                response = self._client.post(f"{self._base_url}/embeddings", json=payload)
            except httpx.HTTPError as exc:
                last_error = f"transport error: {exc}"
                if attempt < _MAX_ATTEMPTS - 1:
                    self._sleep(2**attempt)
                    continue
                raise EmbeddingError(last_error) from exc
            latency = time.monotonic() - started

            if response.status_code in _RETRYABLE_STATUS:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if attempt < _MAX_ATTEMPTS - 1:
                    raw_retry_after = response.headers.get("retry-after")
                    try:
                        # delta-seconds form; the HTTP-date form (RFC 9110)
                        # falls through to exponential backoff
                        delay = float(raw_retry_after) if raw_retry_after else 2.0**attempt
                    except ValueError:
                        delay = 2.0**attempt
                    self._sleep(min(delay, 60.0))
                    continue
                raise EmbeddingError(last_error)
            if response.status_code != 200:
                raise EmbeddingError(
                    f"HTTP {response.status_code} from {self._base_url}/embeddings: "
                    f"{response.text[:500]}"
                )

            try:
                body = response.json()
            except ValueError as exc:
                raise EmbeddingError(
                    f"embedding response is not valid JSON: {response.text[:200]}"
                ) from exc
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list) or len(data) != len(texts):
                raise EmbeddingError(
                    f"embedding response shape mismatch: expected {len(texts)} items, "
                    f"got {len(data) if isinstance(data, list) else type(data)}"
                )
            if not all(
                isinstance(d, dict) and isinstance(d.get("embedding"), list)
                for d in data
            ):
                raise EmbeddingError(
                    "embedding response items are malformed (missing 'embedding' lists)"
                )
            indices = [d.get("index") for d in data]
            if not all(isinstance(i, int) for i in indices) or sorted(
                indices
            ) != list(range(len(texts))):
                raise EmbeddingError(
                    f"embedding response indices are not exactly 0..{len(texts) - 1}: "
                    f"{indices[:10]} — refusing to guess vector-to-text pairing"
                )
            data = sorted(data, key=lambda d: d["index"])
            vectors = [d["embedding"] for d in data]
            usage_raw = body.get("usage")
            usage: dict = usage_raw if isinstance(usage_raw, dict) else {}
            try:
                arr = _np.asarray(vectors, dtype=float)
            except (ValueError, TypeError) as exc:
                raise EmbeddingError(
                    "embedding vectors have inconsistent dimensions across the batch"
                ) from exc
            if arr.ndim != 2:
                raise EmbeddingError(
                    "embedding vectors have inconsistent dimensions across the batch"
                )
            if not _np.isfinite(arr).all():
                raise EmbeddingError("embedding response contains NaN/Inf values")
            stats = EmbedStats(
                model_id=model_id,
                calls=1,
                items=len(texts),
                total_tokens=(
                    int(usage["total_tokens"])
                    if isinstance(usage.get("total_tokens"), int | float)
                    and usage["total_tokens"] >= 0
                    else 0
                ),
                total_latency_s=latency,
                dims=len(vectors[0]) if vectors and vectors[0] else None,
            )
            return EmbedResult(vectors=vectors, stats=stats)
        raise EmbeddingError(last_error)  # pragma: no cover
