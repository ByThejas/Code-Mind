import json

import httpx
import pytest
import respx

from codemind.core.config import Settings
from codemind.core.errors import CodemindError
from codemind.providers import get_answer_model, get_embedding_provider, ollama
from codemind.providers.ollama import OllamaAnswerModel, OllamaEmbeddings

URL = "http://ollama.test:11434"
SETTINGS = Settings(
    ollama_url=URL, embedding_provider="ollama", llm_provider="ollama", embedding_dimensions=3
)
ANSWER = {
    "summary": "It greets.",
    "claims": [{"text": "greet returns a string", "evidence_ids": ["ev_1"], "status": "supported"}],
    "limitations": [],
    "suggested_next_steps": [],
}


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama, "RETRY_DELAYS", (0.0, 0.0))


def test_factories_follow_settings() -> None:
    assert isinstance(get_embedding_provider(SETTINGS), OllamaEmbeddings)
    assert isinstance(get_answer_model(SETTINGS), OllamaAnswerModel)
    defaults = Settings(_env_file=None)  # ignore a developer's local .env
    assert get_embedding_provider(defaults) is None and get_answer_model(defaults) is None


@respx.mock
def test_embed_request_and_dimension_check() -> None:
    route = respx.post(f"{URL}/api/embed").respond(json={"embeddings": [[0.1, 0.2, 0.3]] * 2})
    assert OllamaEmbeddings(SETTINGS).embed(["a", "b"], "document") == [[0.1, 0.2, 0.3]] * 2
    body = json.loads(route.calls.last.request.content)
    assert body == {"model": "bge-m3", "input": ["a", "b"], "truncate": True}

    route.respond(json={"embeddings": [[0.1, 0.2]]})
    with pytest.raises(CodemindError, match="2-dimension vectors"):
        OllamaEmbeddings(SETTINGS).embed(["a"], "query")


@respx.mock
def test_errors_are_mapped() -> None:
    respx.post(f"{URL}/api/embed").respond(status_code=404, json={"error": "model not found"})
    with pytest.raises(CodemindError, match="ollama pull bge-m3") as missing:
        OllamaEmbeddings(SETTINGS).embed(["a"], "document")
    assert missing.value.code == "embedding_provider_error" and missing.value.status_code == 503

    down = respx.post(f"{URL}/api/chat").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(CodemindError, match="not reachable") as unreachable:
        OllamaAnswerModel(SETTINGS).answer("s", "u")
    assert unreachable.value.code == "answer_provider_error" and down.call_count == 3


@respx.mock
def test_chat_retries_once_on_invalid_output() -> None:
    route = respx.post(f"{URL}/api/chat")
    route.side_effect = [
        httpx.Response(200, json={"message": {"content": "not json"}}),
        httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(ANSWER)},
                "prompt_eval_count": 7,
                "eval_count": 3,
            },
        ),
    ]
    result = OllamaAnswerModel(SETTINGS).answer("system", "user")
    assert result.draft.claims[0].evidence_ids == ["ev_1"]
    assert (result.input_tokens, result.output_tokens) == (7, 3)
    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "qwen2.5-coder:7b" and sent["stream"] is False
    assert sent["options"] == {"temperature": 0, "num_ctx": 16384}
    assert set(sent["format"]["required"]) == set(ANSWER)
    assert [m["role"] for m in sent["messages"]] == ["system", "user"]

    route.side_effect = None
    route.respond(json={"message": {"content": "{}"}})
    with pytest.raises(CodemindError) as invalid:
        OllamaAnswerModel(SETTINGS).answer("s", "u")
    assert invalid.value.code == "answer_invalid"
