"""Explicit configuration. Every path is overridable via env (ACP_*)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_control_plane.constants import DEFAULT_TENANT_ID

# src/agent_control_plane/config.py -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ACP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # One control plane instance serves one tenant. A future admin surface
    # constructs several; nothing assumes there is only ever one.
    tenant_id: str = DEFAULT_TENANT_ID

    db_path: Path = Field(default=REPO_ROOT / "data" / "agent_control_plane.db")
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

    # Lets the console seed the deterministic demo dataset when it finds no
    # events, so a visitor to the hosted demo gets a populated page without
    # running a script. On by default because that is what this repo is — a
    # credential-free demo over committed fixtures.
    #
    # Set ACP_DEMO_BOOTSTRAP=false for any deployment that ingests real
    # telemetry: there an empty database means ingestion is broken, and
    # quietly fabricating synthetic rows would hide exactly that.
    demo_bootstrap: bool = True

    @property
    def agents_file(self) -> Path:
        return self.registry_dir / "agents.yaml"

    @property
    def tools_file(self) -> Path:
        return self.registry_dir / "tools.yaml"

    @property
    def data_domains_file(self) -> Path:
        return self.registry_dir / "data_domains.yaml"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def load_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
