from pathlib import Path

import pytest
from pydantic_settings import SettingsConfigDict

from mytube.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


class IsolatedSettings(Settings):
    """Settings isolated from .env AND the real OS environment.

    The unused env_prefix means no exported variable (KNN_K,
    EMBEDDING_MODELS, ...) on a dev machine can leak into test behavior.
    """

    model_config = SettingsConfigDict(env_file=None, env_prefix="MYTUBE_TESTS_IGNORE_")


def make_settings(**overrides) -> Settings:
    return IsolatedSettings(**overrides)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return make_settings(var_dir=tmp_path / "var")
