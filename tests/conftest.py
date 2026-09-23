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
    for f in ("companies.yaml", "answers.example.yaml"):
        shutil.copy(REPO / "config" / f, tmp_path / "config" / f)
    # Pinned, not config/settings.yaml: personal tuning must not change test outcomes.
    shutil.copy(FIXTURES / "settings.test.yaml", tmp_path / "config" / "settings.yaml")

    monkeypatch.setenv("JOBPILOT_ROOT", str(tmp_path))
    config.settings.cache_clear()
    db.reset_engine()
    yield tmp_path
    config.settings.cache_clear()
    db.reset_engine()


class FakeBackend:
    """Stands in for Ollama. `reply(messages) -> str` decides each chat answer.

    Embeddings are a deterministic bag-of-words hash, so texts sharing words
    are closer -- enough to test ranking without a model.
    """

    name = "fake"
    text_model = "fake-text"
    embed_model = "fake-embed"
    DIM = 4096  # wide enough that a whole resume doesn't collide into every bucket

    def __init__(self, reply=None):
        self.reply = reply or (lambda messages: "{}")
        self.chat_calls: list[list[dict]] = []
        self.embed_calls = 0

    async def chat(self, messages, schema, model=None, max_tokens=None):
        self.chat_calls.append(messages)
        self.last_model, self.last_max_tokens = model, max_tokens
        return self.reply(messages)

    async def embed(self, texts):
        import hashlib
        import re

        self.embed_calls += 1
        out = []
        for text in texts:
            vec = [0.0] * self.DIM
            for word in re.findall(r"[a-z]+", text.lower()):
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIM] += 1.0
            out.append(vec)
        return out


@pytest.fixture
def fake_llm(temp_root):
    """An LLMClient over FakeBackend with its cache in the temp DB."""
    from jobpilot import db
    from jobpilot.llm import LLMClient

    db.init_db()
    backend = FakeBackend()
    client = LLMClient(backend=backend, cfg={"max_retries": 1, "log_path": "logs/llm.jsonl"})
    client.fake = backend
    return client
