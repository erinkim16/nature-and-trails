"""Application settings.

Everything configurable lives here and is driven by environment variables (loaded
from `.env`). Nothing else in the codebase reads `os.environ` directly — that keeps
secrets in exactly one place and makes the whole app trivially testable.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/nature_trails/core/config.py
#   parents[3] -> backend/     (the Python app root)
#   parents[4] -> <repo root>  (monorepo root, holds .env and web/)
BACKEND_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_ROOT.parent


class Settings(BaseSettings):
    # Look for `.env` at the monorepo root first (shared with the Next.js app),
    # then fall back to a backend-local one. Later entries win in pydantic-settings,
    # so backend/.env can override the shared file during local debugging.
    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Anthropic -------------------------------------------------------
    # Note: an unset key does NOT necessarily mean "no credentials" — the SDK also
    # picks up `ant auth login` profiles. We leave it optional and let the SDK's own
    # resolution chain run, only warning if nothing at all is available.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")

    model: str = Field(default="claude-opus-5", alias="NT_MODEL")
    research_effort: str = Field(default="high", alias="NT_RESEARCH_EFFORT")
    assembly_effort: str = Field(default="medium", alias="NT_ASSEMBLY_EFFORT")

    # A hard stop on the agentic loop. Without this a confused agent can spin
    # through tool calls until the bill notices.
    max_turns: int = Field(default=30, alias="NT_MAX_TURNS")

    # --- Politeness ------------------------------------------------------
    # OSM's Nominatim usage policy requires an identifying User-Agent with real
    # contact info. Sending a generic one is how projects get IP-banned.
    contact_email: str = Field(default="", alias="NT_CONTACT_EMAIL")

    # --- Caching ---------------------------------------------------------
    cache_path: Path = Field(default=Path("data/http_cache.sqlite"), alias="NT_CACHE_PATH")
    cache_disabled: bool = Field(default=False, alias="NT_CACHE_DISABLED")

    # --- HTTP API --------------------------------------------------------
    # Extra browser origins allowed to call the API, comma-separated. localhost
    # is always permitted for development; add the Vercel domain here in prod.
    cors_origins: str = Field(default="", alias="NT_CORS_ORIGINS")

    # A plan costs real money and runs for minutes. Cap how many can run at once
    # so a burst of requests cannot multiply the bill or exhaust the box.
    max_concurrent_plans: int = Field(default=2, alias="NT_MAX_CONCURRENT_PLANS")

    # Finished jobs are kept in memory so clients can fetch the result. Oldest
    # are evicted past this count — the in-memory store is not a database.
    job_retention: int = Field(default=50, alias="NT_JOB_RETENTION")

    # Wall-clock ceiling on one plan. `max_turns` caps how many turns the agent
    # takes, but says nothing about how long a turn lasts: a spell of DNS or
    # Overpass failures sends every tool call into retry-with-backoff, and a job
    # can occupy a concurrency slot for an hour while barely progressing. Observed
    # for real — see docs/ARCHITECTURE.md §8.5.
    job_timeout_seconds: int = Field(default=900, alias="NT_JOB_TIMEOUT_SECONDS")

    @property
    def allowed_origins(self) -> list[str]:
        base = ["http://localhost:3000", "http://127.0.0.1:3000"]
        extra = [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return base + extra

    @property
    def user_agent(self) -> str:
        contact = self.contact_email or "unset-contact"
        return f"nature-trails/0.1 (+https://github.com/; {contact})"

    @property
    def absolute_cache_path(self) -> Path:
        p = self.cache_path
        return p if p.is_absolute() else BACKEND_ROOT / p


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached accessor so `.env` is parsed exactly once per process."""
    return Settings()
