"""NL steering (FLOW-7, DEC-19, AC-12/13/14).

A directive is parsed by the LLM into signed pseudo-taste terms (embedded
immediately) and steering-origin search-query seeds. Everything is written
in ONE transaction: an LLM or embedding failure aborts with nothing stored
(AC-13). Directives expire after ``steering_ttl_days`` (default 7) or on
explicit cancel; expiry removes all scoring influence and deactivates the
seeded queries (AC-14).
"""

from __future__ import annotations

import numpy as np
import psycopg

from mytube import llm, store
from mytube.config import Settings
from mytube.embeddings import EmbeddingError, OpenRouterEmbedder


class SteeringError(Exception):
    """Directive could not be applied — nothing was stored."""


def create_directive(
    conn: psycopg.Connection,
    settings: Settings,
    user_id: int,
    text: str,
    *,
    embedder: OpenRouterEmbedder | None = None,
) -> dict:
    """Parse + embed + persist one directive atomically. Raises
    SteeringError on any failure; the transaction is rolled back by the
    caller (webapi's get_conn) so no partial state survives (AC-13)."""
    text = text.strip()
    if not text:
        raise SteeringError("steering text must not be empty")
    if len(text) > 1000:
        raise SteeringError("steering text too long (max 1000 chars)")
    # a per-user model choice overrides the server default (NULL = default)
    chosen = conn.execute(
        "SELECT llm_model FROM user_settings WHERE user_id = %s", (user_id,)
    ).fetchone()
    effective = settings
    if chosen and chosen["llm_model"]:
        effective = settings.model_copy(update={"llm_model": chosen["llm_model"]})
    try:
        parsed = llm.generate_steering(effective, text)
    except llm.LLMError as exc:
        raise SteeringError(f"could not parse steering request: {exc}") from exc

    model_id = settings.model_of_record()
    term_texts = [t["text"] for t in parsed["terms"]]
    try:
        embedder = embedder or OpenRouterEmbedder(
            settings.openrouter_api_key, settings.openrouter_base_url
        )
        result = embedder.embed_batch(model_id, term_texts)
    except (EmbeddingError, ValueError) as exc:
        # ValueError: missing API key — same inline-error treatment (AC-13)
        raise SteeringError(f"could not embed steering terms: {exc}") from exc
    if len(result.vectors) != len(term_texts):
        raise SteeringError("embedding count mismatch")

    directive = conn.execute(
        """
        INSERT INTO steering_directives (user_id, raw_text, summary, expires_at)
        VALUES (%s, %s, %s, now() + make_interval(days => %s))
        RETURNING directive_id, summary, created_at, expires_at
        """,
        (user_id, text, parsed["summary"], settings.steering_ttl_days),
    ).fetchone()
    for term, vector in zip(parsed["terms"], result.vectors, strict=True):
        conn.execute(
            """
            INSERT INTO steering_terms
                (directive_id, user_id, term_text, polarity, weight,
                 model_id, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                directive["directive_id"], user_id, term["text"],
                term["polarity"], term["weight"], model_id,
                np.asarray(vector, dtype=np.float32),
            ),
        )
    seeded = 0
    for query in parsed["queries"]:
        # a RE-issued directive must reactivate a seed its predecessor
        # deactivated on expiry — but never retag the user's own manual or
        # taste-derived queries (their lifecycle is not directive-bound)
        cur = conn.execute(
            """
            INSERT INTO search_queries (user_id, query_text, origin, directive_id)
            VALUES (%s, %s, 'steering', %s)
            ON CONFLICT (user_id, query_text) DO UPDATE SET
                active = TRUE, directive_id = EXCLUDED.directive_id
            WHERE search_queries.origin = 'steering'
            """,
            (user_id, query, directive["directive_id"]),
        )
        seeded += cur.rowcount
    return {
        "directive_id": directive["directive_id"],
        "summary": directive["summary"],
        "raw_text": text,
        "created_at": directive["created_at"],
        "expires_at": directive["expires_at"],
        "terms": parsed["terms"],
        "queries_seeded": seeded,
    }


def cancel_directive(
    conn: psycopg.Connection, user_id: int, directive_id: int
) -> bool:
    """Cancel + deactivate seeded queries. Idempotent; False if not found."""
    row = conn.execute(
        """
        UPDATE steering_directives SET cancelled_at = COALESCE(cancelled_at, now())
        WHERE directive_id = %s AND user_id = %s
        RETURNING directive_id
        """,
        (directive_id, user_id),
    ).fetchone()
    if row is None:
        return False
    conn.execute(
        "UPDATE search_queries SET active = FALSE WHERE directive_id = %s",
        (directive_id,),
    )
    return True


def deactivate_expired_queries(conn: psycopg.Connection) -> int:
    """AC-14 upkeep (runs in the bandit_update tick): steering-seeded queries
    stop searching once their directive is expired or cancelled."""
    cur = conn.execute(
        """
        UPDATE search_queries q SET active = FALSE
        FROM steering_directives d
        WHERE q.directive_id = d.directive_id AND q.active
          AND (d.expires_at <= now() OR d.cancelled_at IS NOT NULL)
        """
    )
    return cur.rowcount


def active_terms(conn: psycopg.Connection, user_id: int) -> list[dict]:
    """Pseudo-taste-items for the scorer: only live directives influence
    scores (AC-14). Embeddings normalized to float32 arrays."""
    rows = conn.execute(
        """
        SELECT t.term_id, t.directive_id, t.term_text, t.polarity, t.weight,
               t.model_id, t.embedding, d.created_at
        FROM steering_terms t
        JOIN steering_directives d ON d.directive_id = t.directive_id
        WHERE t.user_id = %s
          AND d.expires_at > now() AND d.cancelled_at IS NULL
        ORDER BY t.term_id
        """,
        (user_id,),
    ).fetchall()
    return [
        dict(r) | {"embedding": store._vector_to_array(r["embedding"])}
        for r in rows
    ]


def list_directives(conn: psycopg.Connection, user_id: int) -> dict:
    """Active + past directives for the UI (AC-12/14)."""
    rows = conn.execute(
        """
        SELECT d.directive_id, d.raw_text, d.summary, d.created_at,
               d.expires_at, d.cancelled_at,
               (d.cancelled_at IS NULL AND d.expires_at > now()) AS active,
               COALESCE(json_agg(json_build_object(
                   'text', t.term_text, 'polarity', t.polarity,
                   'weight', t.weight) ORDER BY t.term_id)
                   FILTER (WHERE t.term_id IS NOT NULL), '[]') AS terms
        FROM steering_directives d
        LEFT JOIN steering_terms t ON t.directive_id = d.directive_id
        WHERE d.user_id = %s
        GROUP BY d.directive_id
        ORDER BY d.created_at DESC
        LIMIT 100
        """,
        (user_id,),
    ).fetchall()
    return {
        "active": [dict(r) for r in rows if r["active"]],
        "past": [dict(r) for r in rows if not r["active"]],
    }
