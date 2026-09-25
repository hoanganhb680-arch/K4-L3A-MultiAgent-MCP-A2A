from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

TEAM_KEY_PATTERN = re.compile(r"^sk-team-[A-Za-z0-9_-]{16,128}$")


@dataclass(frozen=True)
class Settings:
    competition_api_url: str
    team_api_key: str
    mcp_endpoint: str
    root: Path

    @classmethod
    def load(cls, root: Path | None = None) -> Settings:
        resolved_root = (root or Path.cwd()).resolve()
        file_values = dotenv_values(resolved_root / ".env")
        process_key = os.environ.get("COMPETITION_TEAM_API_KEY", "").strip()
        file_key = (file_values.get("COMPETITION_TEAM_API_KEY") or "").strip()
        if process_key and file_key and process_key != file_key:
            raise ValueError(
                "COMPETITION_TEAM_API_KEY differs between process environment and .env. "
                "Refusing to run because this can create cross-team evidence refs."
            )

        def setting(name: str) -> str:
            return (os.environ.get(name) or file_values.get(name) or "").strip()

        api_url = setting("COMPETITION_API_URL").rstrip("/")
        team_key = process_key or file_key
        mcp_endpoint = setting("MCP_ENDPOINT")
        errors: list[str] = []
        if not api_url.startswith(("http://", "https://")):
            errors.append("COMPETITION_API_URL must be an absolute HTTP(S) URL")
        if not TEAM_KEY_PATTERN.fullmatch(team_key):
            errors.append("COMPETITION_TEAM_API_KEY must use the sk-team-... format")
        if not mcp_endpoint.startswith(("http://", "https://")):
            errors.append("MCP_ENDPOINT must be an absolute HTTP(S) URL")
        if errors:
            raise ValueError("; ".join(errors))
        return cls(api_url, team_key, mcp_endpoint, resolved_root)
