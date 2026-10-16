"""Free local models served by Ollama (https://ollama.com): /api/embed and /api/chat.

Nothing leaves the machine running Ollama. Pull the models once with
`ollama pull bge-m3 && ollama pull qwen2.5-coder:7b`.
"""

import logging
import time
from typing import Any

import httpx
from pydantic import ValidationError

from codemind.core.config import Settings
from codemind.core.errors import CodemindError
from codemind.providers import GeneratedAnswer, InputType
from codemind.schemas.retrieval import AnswerDraft

log = logging.getLogger(__name__)

RETRY_DELAYS = (1.0, 4.0)  # transient failures: two retries, then give up


class _OllamaClient:
    def __init__(self, settings: Settings, timeout: float, error_code: str) -> None:
        self.base_url = settings.ollama_url.rstrip("/")
        self.timeout = timeout
        self.error_code = error_code

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                response = httpx.post(f"{self.base_url}{path}", json=body, timeout=self.timeout)
            except httpx.TransportError as exc:
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise self._error(
                    f"Ollama is not reachable at {self.base_url}. Is it running?", retryable=True
                ) from exc
            if response.status_code >= 500 and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                continue
            if response.status_code == 404:
                raise self._error(
                    f"Ollama does not have model {body.get('model')!r}. "
                    f"Run `ollama pull {body.get('model')}`."
                )
            if response.status_code >= 400:
                log.warning("ollama %s returned %s: %s", path, response.status_code, response.text)
                raise self._error(
                    f"Ollama returned HTTP {response.status_code}.",
                    retryable=response.status_code >= 500,
                )
            data: dict[str, Any] = response.json()
            return data
        raise AssertionError("unreachable")

    def _error(self, message: str, *, retryable: bool = False) -> CodemindError:
        return CodemindError(self.error_code, message, status_code=503, retryable=retryable)


class OllamaEmbeddings:
    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.embedding_model
        self.dimensions = settings.embedding_dimensions
        self._client = _OllamaClient(settings, timeout=120, error_code="embedding_provider_error")

    def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        # bge-m3 uses no query/document prefixes, so input_type is not needed here.
        if not texts:
            return []
        data = self._client.post(
            "/api/embed", {"model": self.model_name, "input": texts, "truncate": True}
        )
        vectors: list[list[float]] = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise self._client._error(f"Ollama returned {len(vectors)} vectors for {len(texts)}.")
        if any(len(v) != self.dimensions for v in vectors):
            raise self._client._error(
                f"{self.model_name} returns {len(vectors[0])}-dimension vectors but "
                f"EMBEDDING_DIMENSIONS is {self.dimensions}."
            )
        return vectors


class OllamaAnswerModel:
    def __init__(self, settings: Settings) -> None:
        self.model_name = settings.llm_model
        self.context_tokens = settings.llm_context_tokens
        self._client = _OllamaClient(
            settings, timeout=settings.llm_timeout_seconds, error_code="answer_provider_error"
        )

    def answer(self, system: str, user: str) -> GeneratedAnswer:
        body = {
            "model": self.model_name,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": AnswerDraft.model_json_schema(),
            # Ollama's default context is far smaller than the evidence we send.
            "options": {"temperature": 0, "num_ctx": self.context_tokens},
        }
        for attempt in range(2):  # one retry for output that does not match the schema
            data = self._client.post("/api/chat", body)
            content = (data.get("message") or {}).get("content", "")
            try:
                draft = AnswerDraft.model_validate_json(content)
            except ValidationError:
                log.warning("answer model output failed validation (attempt %s)", attempt + 1)
                continue
            return GeneratedAnswer(
                draft=draft,
                model_name=self.model_name,
                input_tokens=data.get("prompt_eval_count"),
                output_tokens=data.get("eval_count"),
            )
        raise CodemindError(
            "answer_invalid", "The answer model did not return a valid answer.", status_code=502
        )
