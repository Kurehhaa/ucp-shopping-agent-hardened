"""Shared test fixtures for UCP Shopping Agent."""

import pytest

from ucp_shopping.config import Settings
from ucp_shopping.main import build_app


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path):
    """Keep tests independent of the developer's own .env and environment."""
    monkeypatch.chdir(tmp_path)
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def settings():
    """Create test settings."""
    return Settings(
        environment="testing",
        openai_api_key="test-key",
        human_confirmation_required=False,
    )


@pytest.fixture
def app(settings):
    """Create FastAPI app for testing (with mock merchants mounted)."""
    return build_app(settings)
