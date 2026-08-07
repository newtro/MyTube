-- Per-user LLM model selection (steering + query bootstrap). NULL means
-- "use the server default" (settings.llm_model), so an operator changing
-- the default moves every user who hasn't explicitly chosen.
ALTER TABLE user_settings ADD COLUMN llm_model TEXT;
