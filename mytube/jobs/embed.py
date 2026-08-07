"""Embedder job: candidate + taste videos -> vectors for the model of record.

OpenRouter failure policy (§4): the job stops, affected videos simply stay
pending (no embedding row), serving continues from whatever is already
scored, and the backlog is visible on the ops view. The scheduler retries
with backoff; nothing crashes.
"""

from __future__ import annotations

from mytube import benchmark, store
from mytube.embeddings import EmbeddingError, OpenRouterEmbedder
from mytube.scheduler import JobContext

# guard against runaway spend in one tick; the next tick continues
MAX_VIDEOS_PER_TICK = 2000


def embed_pending(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    model_id = settings.model_of_record()
    ids = store.embeddable_video_ids(
        conn, shorts_max_seconds=settings.shorts_max_seconds,
        limit=MAX_VIDEOS_PER_TICK,
    )
    if not ids:
        return {"embedded": 0, "model": model_id}
    embedder = OpenRouterEmbedder(
        settings.openrouter_api_key, settings.openrouter_base_url
    )

    def on_progress(_msg: str) -> None:
        ctx.heartbeat()

    try:
        stats = benchmark.ensure_embeddings(
            conn, embedder, model_id, ids, settings, log=on_progress
        )
    except EmbeddingError as exc:
        # committed batches are safe; the rest stays pending (§4 policy)
        conn.rollback()
        raise exc
    return {"embedded": stats.items, "tokens": stats.total_tokens, "model": model_id}
