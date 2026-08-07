-- Blind-outcome integrity (round 3): exactly one reveal per selection,
-- enforced by the database so concurrent requests can't double-log.
-- Deduplicate first: the pre-fix race window could have double-logged.
DELETE FROM interactions a
USING interactions b
WHERE a.kind = 'blind_reveal' AND b.kind = 'blind_reveal'
  AND a.selection_id = b.selection_id
  AND a.interaction_id > b.interaction_id;

CREATE UNIQUE INDEX interactions_one_reveal_idx
    ON interactions (selection_id) WHERE kind = 'blind_reveal';
