"""Backwards-compatible shim — the real implementation lives in textgen.py.

Kept so existing call sites (`proposal_gen`, `gig_templates`, `orchestrator`)
keep working unchanged. New code should import `generateText` from
`app.textgen` directly.
"""
import json
import re

from .textgen import (LLMRateLimited, LLMTimeout, LLMUnavailable,  # noqa: F401
                      PROMPT_VERSION, generateText, llm_available)


async def complete(system: str, user: str, *, temperature: float | None = None,
                   max_tokens: int | None = None, model: str | None = None,
                   timeout: float | None = None) -> dict:
    return await generateText(system, user, temperature=temperature,
                              max_tokens=max_tokens, model=model, timeout=timeout)


def parse_json_response(text: str) -> dict:
    """Extract a JSON object from an LLM response (tolerates prose fences)."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in LLM response")
    return json.loads(m.group(0))


async def complete_json(system: str, user: str, **kwargs) -> tuple[dict, dict]:
    """Completion parsed as JSON. Returns (parsed, meta)."""
    meta = await complete(system, user + "\n\nRespond with a single JSON object only.", **kwargs)
    return parse_json_response(meta["text"]), meta
