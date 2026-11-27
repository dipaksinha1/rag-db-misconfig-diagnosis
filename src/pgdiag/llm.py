"""OpenRouter client for any model, behind the small part of the Anthropic SDK we use.

Diagnosis (T9) and the judge (T12) call ``client.messages.create(model=, max_tokens=,
system=, messages=)`` and read ``resp.content`` (blocks with ``type``/``text``),
``resp.stop_reason``, ``resp.model`` and ``resp.usage.input_tokens/output_tokens``.
``OpenRouterClient`` offers exactly that, but sends OpenRouter's OpenAI-style
``/chat/completions`` request, which every model on OpenRouter accepts (GPT, gpt-oss,
DeepSeek, Qwen, Claude, ...). Model names are OpenRouter ids, e.g. ``openai/gpt-oss-120b``.

OpenRouter may route one model to different hosts (with different quantization). Each
``Response`` carries ``provider`` (the host that answered) so callers can record it, and
``provider_order`` pins the hosts (no fallback to others) for comparable runs.

Errors: every failure is an ``LLMError`` (a ``DiagnosisError``, so the CLIs and the
experiment handle it like any failed diagnosis). Bad key / no credit / forbidden raise
``LLMFatalError``, which the experiment treats as "stop the run".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from pgdiag.diagnose.errors import DiagnosisError

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
RETRY_STATUS = {408, 409, 500, 502, 503, 504}
FATAL_STATUS = {401, 402, 403}
RATE_LIMIT_RETRIES = 8  # 429s get their own, larger budget
MAX_WAIT_S = 120.0

# OpenAI-style finish_reason -> Anthropic-style stop_reason
_STOP = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}


class LLMError(DiagnosisError):
    pass


class LLMFatalError(LLMError):
    """Retrying cannot help (bad key, no credit, forbidden): the caller should stop."""


class _Retryable(Exception):
    def __init__(self, message: str, wait: float | None = None):
        super().__init__(message)
        self.wait = wait


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


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return max(0.0, float(r.headers.get("retry-after", "")))
    except ValueError:
        return None


def _check_body(data) -> dict:
    """The first choice of a 200 response; errors inside the body are retryable."""
    if not isinstance(data, dict):
        raise _Retryable(f"OpenRouter returned a non-object body: {str(data)[:200]}")
    if data.get("error"):
        raise _Retryable(f"OpenRouter error: {data['error']}")
    choices = data.get("choices") or []
    if not choices:
        raise _Retryable(f"OpenRouter returned no choices: {str(data)[:300]}")
    choice = choices[0]
    if choice.get("error") or choice.get("finish_reason") == "error":
        raise _Retryable(f"provider error: {choice.get('error') or 'finish_reason=error'}")
    return choice


class _Messages:
    def __init__(self, client: OpenRouterClient):
        self._c = client

    def create(self, *, model: str, max_tokens: int, messages: list[dict], system=None):
        chat = [{"role": "system", "content": _text(system)}] if system else []
        chat += [{"role": m["role"], "content": _text(m["content"])} for m in messages]
        body = {"model": model, "max_tokens": max_tokens, "messages": chat}
        if self._c.provider_order:
            body["provider"] = {"order": self._c.provider_order, "allow_fallbacks": False}
        data, choice = self._c.request(body)
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
        provider_order: list[str] | None = None,
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
        self.provider_order = list(provider_order or [])
        self._sleep = sleep
        self.messages = _Messages(self)

    def close(self) -> None:
        self._http.close()

    def request(self, body: dict) -> tuple[dict, dict]:
        """POST /chat/completions with retries; returns (body, first choice)."""
        errors = rate_limited = 0
        while True:
            try:
                r = self._http.post("/chat/completions", json=body)
                if r.status_code == 429:
                    rate_limited += 1
                    if rate_limited > RATE_LIMIT_RETRIES:
                        raise LLMError(f"OpenRouter rate limit (429) persisted: {r.text[:300]}")
                    wait = _retry_after(r)
                    self._sleep(min(MAX_WAIT_S, wait if wait is not None else 5.0 * rate_limited))
                    continue
                if r.status_code in FATAL_STATUS:
                    raise LLMFatalError(f"OpenRouter HTTP {r.status_code}: {r.text[:500]}")
                if r.status_code in RETRY_STATUS:
                    raise _Retryable(f"OpenRouter HTTP {r.status_code}: {r.text[:300]}")
                if r.status_code != 200:
                    raise LLMError(f"OpenRouter HTTP {r.status_code}: {r.text[:500]}")
                try:
                    data = r.json()
                except ValueError as exc:
                    raise _Retryable(f"OpenRouter returned invalid JSON: {r.text[:200]!r}") from exc
                return data, _check_body(data)
            except (httpx.TransportError, _Retryable) as exc:
                errors += 1
                if errors > self.max_retries:
                    raise LLMError(f"OpenRouter request failed: {exc}") from exc
                self._sleep(min(MAX_WAIT_S, 2.0 * 2 ** (errors - 1)))
