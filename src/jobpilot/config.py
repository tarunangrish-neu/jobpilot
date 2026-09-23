"""Loading of the YAML config files under config/."""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    """Repo root, overridable with JOBPILOT_ROOT so tests can redirect it."""
    env = os.environ.get("JOBPILOT_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing config file: {path}. Run `jobpilot init` first."
        )
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@functools.lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    return _load_yaml(project_root() / "config" / "settings.yaml")


def companies() -> list[dict[str, Any]]:
    """Active company board entries from config/companies.yaml."""
    raw = _load_yaml(project_root() / "config" / "companies.yaml")
    entries = raw.get("companies") or []
    out: list[dict[str, Any]] = []
    for entry in entries:
        if not entry.get("active", True):
            continue
        missing = [k for k in ("name", "ats", "token") if not entry.get(k)]
        if missing:
            raise ValueError(
                f"companies.yaml entry {entry!r} is missing required key(s): {missing}"
            )
        out.append(entry)
    return out


def db_path() -> Path:
    return project_root() / "data" / "jobpilot.db"
