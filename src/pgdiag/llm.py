"""OpenRouter client for any model, behind the small part of the Anthropic SDK we use.

Diagnosis (T9) and the judge (T12) call ``client.messages.create(model=, max_tokens=,
system=, messages=)`` and read ``resp.content`` (blocks with ``type``/``text``),
``resp.stop_reason``, ``resp.model`` and ``resp.usage.input_tokens/output_tokens``.
``OpenRouterClient`` offers exactly that, but sends OpenRouter's OpenAI-style
``/chat/completions`` request, which every model on OpenRouter accepts (GPT, gpt-oss,
DeepSeek, Qwen, Claude, ...). Model names are OpenRouter ids, e.g. ``openai/gpt-oss-120b``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}

# OpenAI-style finish_reason -> Anthropic-style stop_reason
_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


class LLMError(RuntimeError):
    pass


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Response:
    content: list[TextBlock]
    stop_reason: str | None
    model: str
    usage: Usage = field(default_factory=Usage)
    provider: str | None = None


def _text(content) -> str:
    """Message content (a string or a list of blocks/dicts) as plain text."""
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        kind = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
        if kind == "text":
            parts.append(b["text"] if isinstance(b, dict) else b.text)
    return "".join(parts)


class _Messages:
    def __init__(self, client: OpenRouterClient):
        self._c = client

    def create(self, *, model: str, max_tokens: int, messages: list[dict], system=None):
        chat = [{"role": "system", "content": _text(system)}] if system else []
        chat += [{"role": m["role"], "content": _text(m["content"])} for m in messages]
        data = self._c.post({"model": model, "max_tokens": max_tokens, "messages": chat})
        if data.get("error"):
            raise LLMError(f"OpenRouter error: {data['error']}")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"OpenRouter returned no choices: {data}")
        choice = choices[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return Response(
            content=[TextBlock(text)] if text else [],
            stop_reason=_STOP.get(choice.get("finish_reason"), choice.get("finish_reason")),
            model=data.get("model") or model,
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            provider=data.get("provider"),
        )


class OpenRouterClient:
    """Minimal ``anthropic.Anthropic`` look-alike that talks to OpenRouter's chat API."""

    def __init__(
        self,
        api_key: str,
        timeout: float = 180.0,
        max_retries: int = 3,
        base_url: str = OPENROUTER_BASE_URL,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ):
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "pgdiag"},
        )
        self.max_retries = max_retries
        self._sleep = sleep
        self.messages = _Messages(self)

    def post(self, body: dict) -> dict:
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                r = self._http.post("/chat/completions", json=body)
            except httpx.TransportError as exc:
                if last:
                    raise LLMError(f"OpenRouter request failed: {exc}") from exc
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code not in RETRY_STATUS or last:
                    raise LLMError(f"OpenRouter HTTP {r.status_code}: {r.text[:500]}")
            self._sleep(min(2.0 * 2**attempt, 30.0))
        raise AssertionError("unreachable")
