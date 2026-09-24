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


def filters_path() -> Path:
    """Your own fetch filters, saved from the Jobs page; they override `filters:` in settings.yaml."""
    return project_root() / "config" / "filters.yaml"


@functools.lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    loaded = _load_yaml(project_root() / "config" / "settings.yaml")
    if filters_path().exists():
        loaded["filters"] = {**(loaded.get("filters") or {}), **_load_yaml(filters_path())}
    return loaded


def save_filters(values: dict[str, Any]) -> None:
    """Write config/filters.yaml and reload settings so the new filters apply at once."""
    path = filters_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Fetch filters saved from the Jobs page. They override `filters:` in settings.yaml.\n"
    path.write_text(header + yaml.safe_dump(values, sort_keys=False, allow_unicode=True), encoding="utf-8")
    settings.cache_clear()


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


def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from .env into os.environ (without overriding what is set).

    API keys (search providers, a hosted LLM) live in the gitignored .env; this
    avoids a python-dotenv dependency for a dozen lines of parsing.
    """
    path = path or project_root() / ".env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("\"'")
        if key and value and key not in os.environ:
            os.environ[key] = value
