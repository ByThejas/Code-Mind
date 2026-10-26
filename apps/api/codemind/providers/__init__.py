"""Model providers behind two small interfaces, chosen by EMBEDDING_PROVIDER and LLM_PROVIDER.

"none" means not configured: indexing skips embeddings, search uses keyword and symbol matches only,
and asking for an answer returns `answer_model_not_configured`.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from codemind.core.config import Settings
from codemind.schemas.patches import PatchDraft
from codemind.schemas.retrieval import AnswerDraft

InputType = Literal["document", "query"]


class EmbeddingProvider(Protocol):
    model_name: str
    dimensions: int

    def embed(self, texts: list[str], input_type: InputType) -> list[list[float]]:
        """One vector per text, in order. Raises CodemindError on provider failure."""
        ...


@dataclass(frozen=True)
class GeneratedAnswer:
    draft: AnswerDraft
    model_name: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class GeneratedPatch:
    draft: PatchDraft
    model_name: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class AnswerModel(Protocol):
    model_name: str

    def answer(self, system: str, user: str) -> GeneratedAnswer:
        """A schema-valid answer draft. Raises CodemindError on provider failure or bad output."""
        ...

    def propose_patch(self, system: str, user: str) -> GeneratedPatch:
        """A schema-valid patch draft. Raises CodemindError on provider failure or bad output."""
        ...


def get_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    if settings.embedding_provider == "ollama":
        from codemind.providers.ollama import OllamaEmbeddings

        return OllamaEmbeddings(settings)
    return None


def get_answer_model(settings: Settings) -> AnswerModel | None:
    if settings.llm_provider == "ollama":
        from codemind.providers.ollama import OllamaAnswerModel

        return OllamaAnswerModel(settings)
    return None
