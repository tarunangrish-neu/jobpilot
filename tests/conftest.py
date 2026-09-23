from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]


def load(name: str):
    with (FIXTURES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def temp_root(tmp_path, monkeypatch):
    """A throwaway project root so DB tests never touch data/jobpilot.db."""
    from jobpilot import config, db

    (tmp_path / "config").mkdir()
    for f in ("settings.yaml", "companies.yaml", "answers.example.yaml"):
        shutil.copy(REPO / "config" / f, tmp_path / "config" / f)

    monkeypatch.setenv("JOBPILOT_ROOT", str(tmp_path))
    config.settings.cache_clear()
    db.reset_engine()
    yield tmp_path
    config.settings.cache_clear()
    db.reset_engine()
