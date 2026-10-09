"""Configuration management for the UCP Shopping Agent."""

from __future__ import annotations

from pydantic import Field

from common.config import Settings as BaseSettings
from ucp_shopping.security import MerchantURLGuard


class Settings(BaseSettings):
    """UCP Shopping Agent configuration.

    Inherits common provider keys and infrastructure settings from
    ``common.config.Settings`` and adds shopping-agent-specific options.
    """

    # Service identity
    service_name: str = "ucp-shopping-agent"
    service_version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 8020

    # LLM configuration
    default_model: str = "gpt-4o-mini"

    # Merchant discovery
    known_merchant_urls: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:8020/merchants/techzone",
            "http://localhost:8020/merchants/homegoods",
            "http://localhost:8020/merchants/megamart",
        ]
    )
    max_merchants: int = 10

    # Timeouts and limits
    comparison_timeout: int = 30
    discovery_timeout: int = 10
    checkout_timeout: int = 60
    max_results_per_merchant: int = 20

    # Human-in-the-loop
    human_confirmation_required: bool = True

    # Session management
    session_ttl_seconds: int = 3600
    max_active_sessions: int = 500

    # Security
    admin_api_key: str = Field(default="", repr=False)
    cors_allow_origins: str = ""  # comma-separated; empty = no cross-origin access
    expose_error_details: bool = False
    allow_private_merchant_hosts: bool = False

    @property
    def cors_origins(self) -> list[str]:
        """Parsed CORS origins."""
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    def merchant_url_guard(self) -> MerchantURLGuard:
        """Guard for outbound merchant requests; configured merchants are trusted."""
        return MerchantURLGuard(
            trusted_urls=self.known_merchant_urls,
            allow_private_hosts=self.allow_private_merchant_hosts,
        )


def get_settings() -> Settings:
    """Return a cached settings instance."""
    return Settings()
