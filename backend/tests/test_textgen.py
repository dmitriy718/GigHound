"""Tests for the textgen service (Ollama provider) and generation endpoints."""
import json

import httpx
import pytest

from app import textgen
from app.textgen import (LLMRateLimited, LLMTimeout, LLMUnavailable,
                         generateText, resolve_provider)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("OLLAMA_BASE_URL", "OLLAMA_MODEL", "OLLAMA_TEMPERATURE",
                "OLLAMA_MAX_TOKENS", "OLLAMA_TIMEOUT", "OLLAMA_REASONING_HEADROOM",
                "LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_MAX_RPM",
                "GIGHOUND_IN_DOCKER"):
        monkeypatch.delenv(var, raising=False)
    # exact-token assertions in most tests assume no reasoning headroom
    monkeypatch.setenv("OLLAMA_REASONING_HEADROOM", "0")
    # fresh bucket per test
    textgen._bucket = textgen._TokenBucket()
    yield


def _mock_transport(handler):
    return httpx.MockTransport(handler)


# ---------------- provider resolution ----------------

def test_default_provider_is_ollama_lan(monkeypatch):
    monkeypatch.setattr(textgen, "_running_in_docker", lambda: False)
    assert resolve_provider() == "ollama"
    assert textgen.ollama_base_url() == "http://192.168.1.68:11434/v1"


def test_default_provider_is_ollama_docker(monkeypatch):
    monkeypatch.setattr(textgen, "_running_in_docker", lambda: True)
    assert textgen.ollama_base_url() == "http://ollama:11434/v1"


def test_env_overrides_url_and_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://10.0.0.5:11434/v1/")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    assert textgen.ollama_base_url() == "http://10.0.0.5:11434/v1"  # trailing slash stripped
    assert textgen.ollama_model() == "qwen3:8b"


def test_explicit_provider_and_api_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    assert resolve_provider() == "openai"
    monkeypatch.delenv("LLM_PROVIDER")
    monkeypatch.setenv("LLM_API_KEY", "sk-x")
    assert resolve_provider() == "openai"
    # Ollama URL beats API key when both set
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://x:11434/v1")
    assert resolve_provider() == "ollama"


def test_tunable_defaults(monkeypatch):
    assert textgen.ollama_temperature() == 0.7
    assert textgen.ollama_max_tokens() == 1024
    assert textgen.ollama_timeout() == 120.0
    monkeypatch.setenv("OLLAMA_TEMPERATURE", "0.3")
    monkeypatch.setenv("OLLAMA_MAX_TOKENS", "512")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "45")
    assert textgen.ollama_temperature() == 0.3
    assert textgen.ollama_max_tokens() == 512
    assert textgen.ollama_timeout() == 45.0


# ---------------- generateText ----------------

@pytest.mark.asyncio
async def test_generate_text_ollama_success(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "qwen3:4b",
            "choices": [{"message": {"content": "generated proposal text"}}],
        })

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    monkeypatch.setenv("OLLAMA_TEMPERATURE", "0.4")
    monkeypatch.setenv("OLLAMA_MAX_TOKENS", "256")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))

    result = await generateText("sys", "user prompt")
    assert result["text"] == "generated proposal text"
    assert result["provider"] == "ollama"
    assert result["model"] == "qwen3:4b"
    assert seen["url"] == "http://ollama.test:11434/v1/chat/completions"
    assert seen["body"]["model"] == "qwen3:4b"
    assert seen["body"]["temperature"] == 0.4
    assert seen["body"]["max_tokens"] == 256
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}


@pytest.mark.asyncio
async def test_generate_text_call_overrides(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    await generateText("s", "u", temperature=0.9, max_tokens=64, model="qwen3:1.7b")
    assert seen["body"]["temperature"] == 0.9
    assert seen["body"]["max_tokens"] == 64
    assert seen["body"]["model"] == "qwen3:1.7b"


@pytest.mark.asyncio
async def test_generate_text_connection_error(monkeypatch):
    def handler(request: httpx.Request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://dead:11434/v1")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    with pytest.raises(LLMUnavailable, match="cannot reach ollama"):
        await generateText("s", "u")


@pytest.mark.asyncio
async def test_generate_text_timeout(monkeypatch):
    def handler(request: httpx.Request):
        raise httpx.ReadTimeout("slow", request=request)

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    with pytest.raises(LLMTimeout):
        await generateText("s", "u", timeout=30)


@pytest.mark.asyncio
async def test_generate_text_http_error(monkeypatch):
    def handler(request: httpx.Request):
        return httpx.Response(404, json={"error": "model 'qwen3:4b' not found"})

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    with pytest.raises(LLMUnavailable, match="HTTP 404"):
        await generateText("s", "u")


@pytest.mark.asyncio
async def test_generate_text_bad_payload_shape(monkeypatch):
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"unexpected": True})

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    with pytest.raises(LLMUnavailable, match="unexpected payload"):
        await generateText("s", "u")


@pytest.mark.asyncio
async def test_rate_limit(monkeypatch):
    monkeypatch.setenv("LLM_MAX_RPM", "2")
    monkeypatch.setattr(textgen.cache, "_r", None)  # isolate from shared Redis counter

    def handler(request: httpx.Request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    await generateText("s", "1")
    await generateText("s", "2")
    with pytest.raises(LLMRateLimited):
        await generateText("s", "3")


@pytest.mark.asyncio
async def test_openai_provider_requires_key(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(LLMUnavailable, match="LLM_API_KEY"):
        await generateText("s", "u")


# ---------------- reasoning-model handling (qwen3 et al.) ----------------

@pytest.mark.asyncio
async def test_think_blocks_stripped(monkeypatch):
    def handler(request: httpx.Request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "<think>long reasoning here</think>\n\nhello"}}],
        })

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    result = await generateText("s", "u")
    assert result["text"] == "hello"


@pytest.mark.asyncio
async def test_reasoning_headroom_applied_for_ollama(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    monkeypatch.setenv("OLLAMA_REASONING_HEADROOM", "6144")  # default behavior
    monkeypatch.setenv("OLLAMA_MAX_TOKENS", "1024")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    await generateText("s", "u")
    assert seen["body"]["max_tokens"] == 7168  # 1024 + 6144 headroom


@pytest.mark.asyncio
async def test_reasoning_only_response_retries_with_headroom(monkeypatch):
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body["max_tokens"])
        if len(calls) == 1:
            # first call: all budget burned on reasoning
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "", "reasoning": "hmm..."},
                             "finish_reason": "length"}],
            })
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "real answer"}}],
        })

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    monkeypatch.setenv("OLLAMA_MAX_TOKENS", "100")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    result = await generateText("s", "u")
    assert result["text"] == "real answer"
    assert calls == [100, 4096]  # retried with 3x headroom (floor 4096)


@pytest.mark.asyncio
async def test_reasoning_only_response_eventually_raises(monkeypatch):
    def handler(request: httpx.Request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning": "still thinking"},
                         "finish_reason": "length"}],
        })

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.test:11434/v1")
    monkeypatch.setenv("OLLAMA_MAX_TOKENS", "100")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=_mock_transport(handler), **kw))
    with pytest.raises(LLMUnavailable, match="reasoning tokens"):
        await generateText("s", "u")
