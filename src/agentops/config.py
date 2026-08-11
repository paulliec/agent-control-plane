"""Explicit configuration. Every path is overridable via env (AGENTOPS_*)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/agentops/config.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_path: Path = Field(default=REPO_ROOT / "data" / "agentops.db")
    registry_dir: Path = Field(default=REPO_ROOT / "config")
    data_dir: Path = Field(default=REPO_ROOT / "data")

    log_level: str = "INFO"
    log_json: bool = Field(
        default=False, description="Emit logs as JSON lines instead of key=value."
    )

    # Fallback governance for agents that appear in events but not the registry.
    # Kept low so shadow agents surface as findings rather than pass silently.
    default_max_cost_usd: float = 1.00
    default_max_tool_calls: int = 25

    @property
    def agents_file(self) -> Path:
        return self.registry_dir / "agents.yaml"

    @property
    def tools_file(self) -> Path:
        return self.registry_dir / "tools.yaml"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
