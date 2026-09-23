"""Provider abstraction for every LLM and embedding call.

All model traffic goes through `LLMClient`, which owns the cross-cutting
rules from the spec: a timeout, retry with backoff, JSON mode, pydantic
validation (with one corrective retry on invalid output), a SQLite cache
keyed by input hash, and a JSONL prompt/response log for debugging.

Two backends:
  * `ollama` (default) -- local models via the ollama Python client.
  * `openai_compatible` -- Groq / Gemini / OpenRouter style chat-completions
    endpoints, reached through PoliteClient so they get rate limiting too.

Throughput: a local Ollama server runs about one generation at a time, so
sending it more requests only makes them queue -- and a queued request's
timeout clock is already running. `LLMSlots` caps in-flight calls at
`llm.concurrency` *across every jobpilot process* (lock files under
.cache/llm_slots), so a request is only sent when a slot is free and the
timeout measures model time, not waiting time. Two pipelines started at
once now take turns instead of timing each other out.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .. import config, db
from ..models import Embedding, LLMCache, utcnow
from .prompts import Prompt

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not produce a valid answer within the retry budget."""


class Backend(Protocol):
    name: str
    text_model: str
    embed_model: str

    async def chat(
        self,
        messages: list[dict[str, str]],
        schema: Optional[dict],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# --- cross-process concurrency ---------------------------------------------------


class LLMSlots:
    """At most `n` model calls in flight across all processes sharing this project."""

    def __init__(self, n: int, directory: Path) -> None:
        self.n = max(1, n)
        self.directory = directory
        self._local = asyncio.Semaphore(self.n)

    def _try_lock(self) -> Optional[Any]:
        self.directory.mkdir(parents=True, exist_ok=True)
        for i in range(self.n):
            fh = open(self.directory / f"slot-{i}.lock", "a")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except BlockingIOError:
                fh.close()
        return None

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[float]:
        """Yields how long we waited for the slot, in seconds."""
        started = time.monotonic()
        async with self._local:
            fh = self._try_lock()
            while fh is None:
                await asyncio.sleep(0.25)
                fh = self._try_lock()
            try:
                yield time.monotonic() - started
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()


# --- backends --------------------------------------------------------------


class OllamaBackend:
    name = "ollama"

    def __init__(self, cfg: dict[str, Any]) -> None:
        import ollama

        self.text_model = cfg.get("text_model", "qwen2.5:7b-instruct")
        self.embed_model = cfg.get("embed_model", "nomic-embed-text")
        # Keep models resident between stages; reloading qwen costs seconds per stage.
        self.keep_alive = cfg.get("keep_alive", "30m")
        self._client = ollama.AsyncClient(
            host=os.environ.get("OLLAMA_HOST"), timeout=float(cfg.get("timeout_seconds", 120))
        )

    async def chat(self, messages, schema, model=None, max_tokens=None) -> str:
        options: dict[str, Any] = {"temperature": 0}
        if max_tokens:
            options["num_predict"] = max_tokens
        resp = await self._client.chat(
            model=model or self.text_model,
            messages=messages,
            # A JSON schema constrains decoding; plain "json" is the fallback.
            format=schema or "json",
            options=options,
            keep_alive=self.keep_alive,
        )
        return resp.message.content or ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        resp = await self._client.embed(model=self.embed_model, input=texts, keep_alive=self.keep_alive)
        return [list(v) for v in resp.embeddings]


class OpenAICompatibleBackend:
    name = "openai_compatible"

    def __init__(self, cfg: dict[str, Any]) -> None:
        self.text_model = cfg.get("text_model", "")
        self.embed_model = cfg.get("embed_model", "")
        self.base_url = os.environ.get("JOBPILOT_LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.environ.get("JOBPILOT_LLM_API_KEY", "")
        if not self.base_url or not self.api_key:
            raise LLMError(
                "llm.provider is openai_compatible but JOBPILOT_LLM_BASE_URL / "
                "JOBPILOT_LLM_API_KEY are not set (see .env.example)"
            )

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        from ..http import PoliteClient

        headers = {"Authorization": f"Bearer {self.api_key}"}
        async with PoliteClient(use_cache=False) as client:
            status, body = await client.post_json(f"{self.base_url}{path}", payload, headers)
        if status != 200 or body is None:
            raise LLMError(f"{path} returned HTTP {status}: {str(body)[:300]}")
        return body

    async def chat(self, messages, schema, model=None, max_tokens=None) -> str:
        payload: dict[str, Any] = {
            "model": model or self.text_model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = await self._post("/chat/completions", payload)
        return body["choices"][0]["message"]["content"] or ""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        body = await self._post("/embeddings", {"model": self.embed_model, "input": texts})
        return [row["embedding"] for row in sorted(body["data"], key=lambda r: r["index"])]


def make_backend(cfg: Optional[dict[str, Any]] = None) -> Backend:
    cfg = cfg if cfg is not None else config.settings().get("llm", {})
    provider = cfg.get("provider", "ollama")
    if provider == "ollama":
        return OllamaBackend(cfg)
    if provider == "openai_compatible":
        return OpenAICompatibleBackend(cfg)
    raise LLMError(f"unknown llm.provider '{provider}'")


# --- client ----------------------------------------------------------------


def _extract_json(raw: str) -> Any:
    """Parse a model reply, tolerating a ```json fence or leading prose."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class LLMClient:
    def __init__(self, backend: Optional[Backend] = None, cfg: Optional[dict] = None) -> None:
        cfg = cfg if cfg is not None else config.settings().get("llm", {})
        self.backend = backend or make_backend(cfg)
        self.max_retries = int(cfg.get("max_retries", 2))
        self.log_path = config.project_root() / cfg.get("log_path", "logs/llm.jsonl")
        # Per-task model overrides, e.g. {visa_check: "qwen2.5:3b"}: small, fast models for
        # classification; the default text model for scoring and tailoring.
        self.task_models: dict[str, str] = dict(cfg.get("task_models") or {})
        self.slots = LLMSlots(
            int(cfg.get("concurrency", 1)), config.project_root() / ".cache" / "llm_slots"
        )

    def task_model(self, prompt: Prompt) -> str:
        return self.task_models.get(prompt.name) or self.backend.text_model

    # --- logging / cache -------------------------------------------------

    def _log(self, record: dict[str, Any]) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": utcnow().isoformat(), **record}, default=str) + "\n")
        except OSError as exc:
            log.warning("could not write LLM log: %s", exc)

    def _cache_key(self, prompt: Prompt, messages: list[dict[str, str]]) -> str:
        basis = json.dumps(
            [self.backend.name, self.task_model(prompt), prompt.name, prompt.version, messages],
            sort_keys=True,
        )
        return hashlib.sha256(basis.encode()).hexdigest()

    @staticmethod
    def _cache_get(key: str) -> Optional[str]:
        with db.session() as sess:
            row = sess.get(LLMCache, key)
            return row.response if row else None

    @staticmethod
    def _cache_put(key: str, prompt: str, response: str) -> None:
        with db.session() as sess:
            sess.merge(LLMCache(key=key, prompt=prompt, response=response, created_at=utcnow()))
            sess.commit()

    # --- public API ------------------------------------------------------

    async def complete_json(
        self, prompt: Prompt, schema: type[T], use_cache: bool = True, **variables: Any
    ) -> T:
        """Run a versioned prompt and return a validated `schema` instance.

        Transport errors retry with exponential backoff; invalid JSON or a
        schema mismatch retries with the validation error fed back to the
        model. Raises LLMError when the budget is exhausted.
        """
        messages = prompt.render(**variables)
        key = self._cache_key(prompt, messages)
        if use_cache:
            cached = self._cache_get(key)
            if cached is not None:
                try:
                    return schema.model_validate_json(cached)
                except ValidationError:
                    pass  # schema changed since this was cached; recompute

        model = self.task_model(prompt)
        json_schema = schema.model_json_schema()
        convo = list(messages)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            raw, queued, model_s = "", 0.0, 0.0
            try:
                async with self.slots.slot() as queued:
                    started = time.monotonic()
                    try:
                        raw = await self.backend.chat(convo, json_schema, model=model, max_tokens=prompt.max_tokens)
                    finally:
                        model_s = time.monotonic() - started
                parsed = schema.model_validate(_extract_json(raw))
            except (json.JSONDecodeError, ValidationError) as exc:
                last_error = f"invalid output: {exc}"
                convo = messages + [
                    {"role": "assistant", "content": raw},
                    {
                        "role": "user",
                        "content": "That reply was not valid JSON matching the required "
                        f"schema ({str(exc)[:400]}). Reply with only the corrected JSON object.",
                    },
                ]
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - transport errors vary by backend
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(min(2**attempt, 8))
            else:
                self._log(
                    {
                        "prompt": prompt.name,
                        "version": prompt.version,
                        "model": model,
                        "key": key,
                        "messages": convo,
                        "response": raw,
                        "seconds": round(queued + model_s, 2),
                        "queued_s": round(queued, 2),
                        "model_s": round(model_s, 2),
                    }
                )
                self._cache_put(key, prompt.name, parsed.model_dump_json())
                return parsed
            self._log(
                {
                    "prompt": prompt.name,
                    "version": prompt.version,
                    "model": model,
                    "key": key,
                    "messages": convo,
                    "response": raw,
                    "error": last_error,
                    "attempt": attempt,
                    "queued_s": round(queued, 2),
                    "model_s": round(model_s, 2),
                }
            )
        raise LLMError(f"{prompt.name}: {last_error}")

    async def embed(self, texts: list[str], batch_size: int = 32) -> list[list[float]]:
        """Embed texts, serving repeats from the `embeddings` table."""
        model = self.backend.embed_model
        keys = [hashlib.sha256(f"{model}\n{t}".encode()).hexdigest() for t in texts]
        found: dict[str, list[float]] = {}
        with db.session() as sess:
            for key in set(keys):
                row = sess.get(Embedding, key)
                if row is not None:
                    found[key] = json.loads(row.vector_json)

        missing = [(k, t) for k, t in zip(keys, texts) if k not in found]
        # Dedupe within the batch so identical descriptions embed once.
        missing = list(dict(missing).items())
        for i in range(0, len(missing), batch_size):
            chunk = missing[i : i + batch_size]
            async with self.slots.slot():
                vectors = await self.backend.embed([t for _, t in chunk])
            with db.session() as sess:
                for (key, _), vec in zip(chunk, vectors):
                    found[key] = vec
                    sess.merge(
                        Embedding(key=key, model=model, vector_json=json.dumps(vec), created_at=utcnow())
                    )
                sess.commit()
        return [found[k] for k in keys]
