"""Settings, read from the environment.

Agent model choice is deliberately NOT here - it lives in the ``agent_configs``
table so it can change with an UPDATE instead of a deploy. See
docs/ARCHITECTURE.md section 4.0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = ""
    anthropic_api_key: str | None = None
    # Single-user mode for phases 1-3: no auth, and the one user is the owner,
    # so personal-tier sources are available. Must be false once anyone else
    # can log in.
    single_user_mode: bool = True
    environment: str = "development"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            single_user_mode=_flag("SINGLE_USER_MODE", True),
            environment=os.getenv("ENVIRONMENT", "development"),
        )

    @property
    def is_owner_request(self) -> bool:
        """Who the current caller is, while auth does not exist yet.

        In single-user mode the only caller is the owner. Phase 6 replaces
        this with the authenticated user's ``is_owner`` column, and this
        property goes away rather than becoming a backdoor.
        """
        return self.single_user_mode


settings = Settings.from_env()
