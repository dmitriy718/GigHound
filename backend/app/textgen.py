"""Reusable text generation service — `generateText()`.

Provider resolution (first match wins):
  1. LLM_PROVIDER=ollama|openai   — explicit choice
  2. OLLAMA_BASE_URL set          — Ollama (local/self-hosted, OpenAI-compatible /v1)
  3. LLM_API_KEY set              — hosted OpenAI-compatible provider
  4. fallback: Ollama with environment-based default URL:
       - Docker container:  http://ollama:11434/v1
       - local machine:     http://localhost:11434/v1
     Anything else (remote Ollama host, LAN box, …) must set OLLAMA_BASE_URL
     explicitly.

Ollama configuration (all env-driven, nothing hardcoded in call sites):
  OLLAMA_BASE_URL     — see defaults above
  OLLAMA_MODEL        — default qwen3:4b
  OLLAMA_TEMPERATURE  — default 0.7
  OLLAMA_MAX_TOKENS   — default 1024
  OLLAMA_TIMEOUT      — seconds, default 120 (local models can be slow)

Tuning overrides per call: generateText(..., temperature=, max_tokens=, timeout=).
"""
import logging
import os
import time

import httpx

from .cache import cache

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1.1"

# --- env config (resolved lazily so tests can monkeypatch) ---

_DOCKER_OLLAMA_DEFAULT = "http://ollama:11434/v1"
_LOCAL_OLLAMA_DEFAULT = "http://localhost:11434/v1"


def _running_in_docker() -> bool:
    return os.path.exists("/.dockerenv") or os.getenv("GIGHOUND_IN_DOCKER") == "1"


def ollama_base_url() -> str:
    url = os.getenv("OLLAMA_BASE_URL")
    if url:
        return url.rstrip("/")
    return (_DOCKER_OLLAMA_DEFAULT if _running_in_docker() else _LOCAL_OLLAMA_DEFAULT)


def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "qwen3:4b")


def ollama_temperature() -> float:
    return float(os.getenv("OLLAMA_TEMPERATURE", "0.7"))


def ollama_max_tokens() -> int:
    return int(os.getenv("OLLAMA_MAX_TOKENS", "1024"))


def ollama_timeout() -> float:
    return float(os.getenv("OLLAMA_TIMEOUT", "120"))


def resolve_provider() -> str:
    """Returns 'ollama' | 'openai' | 'none'."""
    explicit = os.getenv("LLM_PROVIDER", "").lower()
    if explicit in ("ollama", "openai"):
        return explicit
    if os.getenv("OLLAMA_BASE_URL"):
        return "ollama"
    if os.getenv("LLM_API_KEY"):
        return "openai"
    return "ollama"  # default: local Ollama with environment-based URL


# --- errors ---

class LLMUnavailable(Exception):
    pass


class LLMRateLimited(Exception):
    pass


class LLMTimeout(LLMUnavailable):
    pass


# --- rate limiting (shared Redis token bucket, in-process fallback) ---

class _TokenBucket:
    KEY = "llm:tokens"

    def __init__(self):
        self._local_count = 0
        self._local_window = 0

    def try_acquire(self, rpm: int) -> bool:
        window = int(time.time() // 60)
        if cache._r is not None:
            key = f"{self.KEY}:{window}"
            count = cache._r.incr(key)
            if count == 1:
                cache._r.expire(key, 120)
            return count <= rpm
        if window != self._local_window:
            self._local_window = window
            self._local_count = 0
        self._local_count += 1
        return self._local_count <= rpm


_bucket = _TokenBucket()


def llm_available() -> bool:
    return resolve_provider() != "none"


# --- the service ---

async def generateText(
    system: str,
    user: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    model: str | None = None,
) -> dict:
    """One chat completion through the resolved provider.

    Returns {"text", "model", "provider", "latency_ms"}.
    Raises LLMUnavailable (connection/HTTP errors), LLMTimeout, LLMRateLimited.
    """
    provider = resolve_provider()
    rpm = int(os.getenv("LLM_MAX_RPM", "60"))
    if not _bucket.try_acquire(rpm):
        raise LLMRateLimited(f"LLM rate limit ({rpm}/min) exceeded")

    if provider == "ollama":
        base = ollama_base_url()
        use_model = model or ollama_model()
        temp = ollama_temperature() if temperature is None else temperature
        tokens = ollama_max_tokens() if max_tokens is None else max_tokens
        wait = ollama_timeout() if timeout is None else timeout
        headers = {"Authorization": "Bearer ollama"}  # Ollama ignores it; keeps /v1 compat
        # Reasoning models (qwen3, …) burn tokens on hidden reasoning inside the
        # same budget — add configurable headroom so the visible answer fits.
        headroom = int(os.getenv("OLLAMA_REASONING_HEADROOM", "6144"))
        request_tokens = tokens + headroom
    elif provider == "openai":
        api_key = os.getenv("LLM_API_KEY")
        if not api_key:
            raise LLMUnavailable("LLM_PROVIDER=openai but LLM_API_KEY is not set")
        base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        use_model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        temp = 0.7 if temperature is None else temperature
        tokens = 1200 if max_tokens is None else max_tokens
        wait = 30.0 if timeout is None else timeout
        headers = {"Authorization": f"Bearer {api_key}"}
    else:
        raise LLMUnavailable("no LLM provider configured")
    if provider != "ollama":
        request_tokens = tokens

    start = time.monotonic()
    try:
        # connect timeout stays short so a dead Ollama fails fast and callers
        # can fall back; the full `wait` budget applies to generation
        timeout_cfg = httpx.Timeout(timeout=wait, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout_cfg) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                headers=headers,
                json={
                    "model": use_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temp,
                    "max_tokens": request_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException as exc:
        raise LLMTimeout(f"{provider} timed out after {wait}s") from exc
    except httpx.ConnectError as exc:
        raise LLMUnavailable(f"cannot reach {provider} at {base}: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise LLMUnavailable(
            f"{provider} HTTP {exc.response.status_code}: {exc.response.text[:300]}"
        ) from exc

    latency_ms = int((time.monotonic() - start) * 1000)
    try:
        choice = data["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable(f"{provider} returned an unexpected payload shape") from exc

    # Reasoning models (e.g. qwen3): thinking may be embedded as
    # <think>...</think> or returned in a separate `reasoning` field while
    # consuming the whole max_tokens budget (content empty, finish_reason
    # = "length"). Strip think blocks; retry once with 3x headroom if the
    # visible answer is empty.
    text = _strip_think_blocks(text)
    if not text.strip() and choice.get("message", {}).get("reasoning"):
        if max_tokens is None and tokens < 4096:
            retry_tokens = max(tokens * 3, 4096)
            log.info("%s returned only reasoning tokens; retrying with %d tokens",
                     provider, retry_tokens)
            return await generateText(system, user, temperature=temperature,
                                      max_tokens=retry_tokens, timeout=timeout, model=model)
        raise LLMUnavailable(
            f"{provider} model '{use_model}' returned only reasoning tokens — "
            "increase OLLAMA_MAX_TOKENS or use a non-reasoning model variant"
        )
    return {
        "text": text,
        "model": data.get("model", use_model),
        "provider": provider,
        "latency_ms": latency_ms,
    }


def _strip_think_blocks(text: str) -> str:
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
