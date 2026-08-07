"""Settings loading — including the exact .env formats the docs prescribe.

Regression tests for the review blocker: pydantic-settings JSON-decodes
complex fields from env sources BEFORE validators run, so list fields need
NoDecode for the documented comma-separated format to work at all.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from mytube.config import Settings

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """These tests exercise real env-file loading; exported vars on a dev
    machine (DATABASE_URL, EMBEDDING_MODELS, ...) must not leak in."""
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


def settings_from_env_file(tmp_path, content: str) -> Settings:
    env = tmp_path / ".env"
    env.write_text(content, encoding="utf-8")
    return Settings(_env_file=env)


class TestEnvFileFormats:
    def test_comma_separated_models(self, tmp_path):
        s = settings_from_env_file(
            tmp_path, "EMBEDDING_MODELS=model-a,model-b , model-c\n"
        )
        assert s.embedding_models == ["model-a", "model-b", "model-c"]

    def test_json_array_models(self, tmp_path):
        s = settings_from_env_file(
            tmp_path, 'EMBEDDING_MODELS=["model-a", "model-b"]\n'
        )
        assert s.embedding_models == ["model-a", "model-b"]

    def test_single_model_without_comma(self, tmp_path):
        s = settings_from_env_file(tmp_path, "EMBEDDING_MODELS=only-one\n")
        assert s.embedding_models == ["only-one"]

    def test_duplicates_collapsed(self, tmp_path):
        s = settings_from_env_file(tmp_path, "EMBEDDING_MODELS=a,b,a\n")
        assert s.embedding_models == ["a", "b"]

    def test_shipped_env_example_loads(self):
        """The literal `cp .env.example .env` quickstart path must work."""
        example = REPO_ROOT / ".env.example"
        s = Settings(_env_file=example)
        assert len(s.embedding_models) >= 2      # the gate needs >=2 models
        assert s.shorts_max_seconds == 180
        assert s.database_url.startswith("postgresql://")

    def test_prices_json_object(self, tmp_path):
        s = settings_from_env_file(
            tmp_path, 'EMBEDDING_PRICES_PER_MTOK={"m": 0.02}\n'
        )
        assert s.embedding_prices_per_mtok == {"m": 0.02}

    def test_negative_price_rejected(self, tmp_path):
        with pytest.raises(ValidationError):
            settings_from_env_file(
                tmp_path, 'EMBEDDING_PRICES_PER_MTOK={"m": -1}\n'
            )


class TestDomainValidation:
    @pytest.mark.parametrize(
        "field, value",
        [
            ("knn_k", 0),
            ("knn_negative_k", 0),
            ("taste_half_life_days", 0),
            ("shorts_max_seconds", 0),
            ("embed_batch_size", 0),
            ("taste_like_weight", 0),
            ("negative_penalty_weight", -1),
            ("benchmark_candidate_limit", 10),
        ],
    )
    def test_out_of_domain_rejected(self, field, value):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{field: value})
