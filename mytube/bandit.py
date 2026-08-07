"""Thompson-sampling search allocator (DEC-20, AC-21).

Each query is a Beta(alpha, beta) arm. A search slot draws θ per arm and
runs the argmax — except a ~20% speculative reservation that picks the
least-recently-searched arm uniformly, so new/quiet queries always get
airtime. Reward: a trial's DISCOVERED video (first-touch, exclusive)
reaching completion or a like within the 14-day window → alpha += 1, at
most once per (query, video). A trial closing with zero reward is censored
→ beta += 1.
"""

from __future__ import annotations

import random

import psycopg

from mytube.config import Settings


def select_queries(
    conn: psycopg.Connection,
    settings: Settings,
    user_id: int,
    slots: int,
    *,
    rng: random.Random | None = None,
) -> list[dict]:
    """Pick up to ``slots`` distinct queries for this tick.

    Returns rows with an added ``speculative`` bool. Deterministic given a
    seeded rng (the AC-21 fixture surface).
    """
    rng = rng or random.Random()
    arms = conn.execute(
        """
        SELECT q.query_id, q.query_text, q.alpha, q.beta, q.last_searched_at
        FROM search_queries q
        LEFT JOIN steering_directives d ON d.directive_id = q.directive_id
        WHERE q.user_id = %s AND q.active
          -- a steering seed stops the moment its directive dies (AC-14) —
          -- the bandit tick's deactivation is just cleanup, not the gate
          AND (q.directive_id IS NULL
               OR (d.expires_at > now() AND d.cancelled_at IS NULL))
        ORDER BY q.query_id
        """,
        (user_id,),
    ).fetchall()
    if not arms:
        return []
    chosen: list[dict] = []
    remaining = [dict(a) for a in arms]
    for _slot in range(min(slots, len(remaining))):
        if rng.random() < settings.speculative_fraction:
            # speculative reservation: uniform over the least-recently-
            # searched half (never-searched first)
            pool = sorted(
                remaining,
                key=lambda a: (
                    a["last_searched_at"] is not None,
                    a["last_searched_at"] or 0,
                    a["query_id"],
                ),
            )
            pool = pool[: max(1, len(pool) // 2)]
            pick = pool[rng.randrange(len(pool))]
            pick = dict(pick) | {"speculative": True}
        else:
            samples = [
                (rng.betavariate(a["alpha"], a["beta"]), a["query_id"], a)
                for a in remaining
            ]
            samples.sort(key=lambda s: (-s[0], s[1]))
            pick = dict(samples[0][2]) | {"speculative": False}
        chosen.append(pick)
        remaining = [a for a in remaining if a["query_id"] != pick["query_id"]]
    return chosen


def apply_rewards(conn: psycopg.Connection, settings: Settings) -> dict:
    """FLOW-16: credit open trials whose discoveries earned a completion or
    like inside the attribution window; close expired trials (censored →
    beta += 1 when the trial earned nothing). Idempotent per (query, video)
    via query_rewards' primary key."""
    rewarded = conn.execute(
        """
        WITH earned AS (
            SELECT DISTINCT t.query_id, t.trial_id, d.video_id
            FROM search_trials t
            JOIN search_discoveries d ON d.trial_id = t.trial_id
            -- no now() bound: an in-window reward must still credit when the
            -- job first runs AFTER the window expired (the event bounds below
            -- enforce the window; the censor pass runs second, so the trial
            -- then closes rewarded instead of penalized)
            WHERE NOT t.closed
              AND (
                  EXISTS (
                      SELECT 1 FROM watch_events w
                      WHERE w.user_id = t.user_id AND w.video_id = d.video_id
                        AND w.source = 'player'
                        -- player events are day-bucketed to local midnight,
                        -- so a same-day watch stamps BEFORE executed_at; the
                        -- 1-day tolerance is safe because discoveries are
                        -- first-touch (no player watch can predate the trial
                        -- that introduced the video)
                        AND w.watched_at >= t.executed_at - interval '1 day'
                        AND w.watched_at <= t.window_closes_at
                  )
                  OR EXISTS (
                      SELECT 1 FROM like_events l
                      WHERE l.user_id = t.user_id AND l.video_id = d.video_id
                        -- only likes with a KNOWN like-time count: the API
                        -- likes sync stores liked_at=NULL (YouTube doesn't
                        -- expose like time), so its insert time can't tell a
                        -- fresh like from one made years before the search —
                        -- crediting those would over-attribute. Takeout likes
                        -- carry real (date-granular) timestamps, hence the
                        -- same 1-day tolerance as watches. Player watches
                        -- remain the primary reward signal.
                        AND l.liked_at IS NOT NULL
                        AND l.liked_at >= t.executed_at - interval '1 day'
                        AND l.liked_at <= t.window_closes_at
                  )
              )
        ),
        inserted AS (
            INSERT INTO query_rewards (query_id, video_id)
            SELECT query_id, video_id FROM earned
            ON CONFLICT (query_id, video_id) DO NOTHING
            RETURNING query_id, video_id
        ),
        bumped AS (
            UPDATE search_queries q
            SET alpha = q.alpha + r.n, total_reward = q.total_reward + r.n
            FROM (SELECT query_id, count(*) AS n FROM inserted GROUP BY query_id) r
            WHERE q.query_id = r.query_id
            RETURNING q.query_id
        ),
        trial_credit AS (
            UPDATE search_trials t
            SET reward = t.reward + c.n
            FROM (
                SELECT e.trial_id, count(*) AS n
                FROM earned e
                JOIN inserted i ON i.query_id = e.query_id
                                AND i.video_id = e.video_id
                GROUP BY e.trial_id
            ) c
            WHERE t.trial_id = c.trial_id
            RETURNING t.trial_id
        )
        SELECT count(*) AS n FROM inserted
        """
    ).fetchone()["n"]

    censored = conn.execute(
        """
        WITH expired AS (
            UPDATE search_trials
            SET closed = TRUE
            WHERE NOT closed AND window_closes_at < now()
            RETURNING trial_id, query_id, reward
        ),
        penalized AS (
            UPDATE search_queries q
            SET beta = q.beta + z.n
            FROM (SELECT query_id, count(*) AS n FROM expired
                  WHERE reward = 0 GROUP BY query_id) z
            WHERE q.query_id = z.query_id
            RETURNING q.query_id
        )
        SELECT count(*) AS n FROM expired
        """
    ).fetchone()["n"]
    return {"rewards_applied": rewarded, "trials_closed": censored}
