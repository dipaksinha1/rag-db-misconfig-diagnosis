"""Diagnosis step (T9): symptom document (+ retrieved chunks) -> LLM -> 4 ranked fixes.

Both modes use one prompt template; RAG mode only adds the "Reference documentation" block.
The prompt never names candidate settings, case ids or file names. The answer must be JSON
(a fenced ```json block is tolerated); invalid answers are sent back to the model with the
error, up to ``MAX_REPAIRS`` times, then ``DiagnosisError`` is raised.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any

from pgdiag.config import get_config
from pgdiag.diagnose.errors import DiagnosisError

# The model thinks before answering and thinking counts against this. Was 4000 until the
# OpenRouter client: reasoning models (gpt-oss) may think longer than Claude.
MAX_TOKENS = 8000
TIMEOUT_S = 180.0
SDK_RETRIES = 3  # transient errors (429, 5xx, connection), handled by the SDK
MAX_REPAIRS = 2  # extra attempts after invalid output
N_FIXES = 4

SYSTEM = "You are an experienced PostgreSQL 16 database administrator."

TEMPLATE = """\
A PostgreSQL 16 server has become slow. Below is a symptom report collected from it while it \
ran its usual workload: configuration values, workload timings, statement statistics, \
database, table and I/O statistics, a log summary and EXPLAIN ANALYZE plans.

Find the {n} server configuration parameters most likely to be misconfigured and causing the \
problem, ranked from most to least likely, and give a concrete value to set each one to.

{docs}Symptom report (JSON):
{symptoms}

Answer with JSON only, no other text, in exactly this form:
{{"fixes": [{{"rank": 1, "setting": "<parameter name>", "proposed_value": "<value>", \
"reasoning": "<one to three sentences>", "cited_chunks": ["<chunk id>"]}}, ...]}}

Rules: exactly {n} fixes with ranks 1 to {n}, each a different parameter; "setting" is the \
exact PostgreSQL parameter name; "proposed_value" is a value you could put in \
postgresql.conf. "cited_chunks" lists the ids (in square brackets above) of reference \
documentation chunks that support the fix; it must be an empty list when no reference \
documentation is given.
"""

DOCS_HEADER = "Reference documentation:\n\n"

_SETTING = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _chunk_dict(chunk: Any) -> dict:
    return asdict(chunk) if is_dataclass(chunk) else dict(chunk)


def docs_block(chunks: list[dict]) -> str:
    """The RAG-only block: ``[chunk_id] title`` then the text, per chunk."""
    if not chunks:
        return ""
    body = "\n\n".join(f"[{c['chunk_id']}] {c['title']}\n{c['text']}" for c in chunks)
    return DOCS_HEADER + body + "\n\n"


def build_prompt(symptoms: dict, chunks: list[dict] | None = None) -> str:
    return TEMPLATE.format(
        n=N_FIXES, docs=docs_block(chunks or []), symptoms=json.dumps(symptoms, indent=1)
    )


def parse_fixes(text: str, allowed_ids: set[str] | None) -> tuple[list[dict], list[str]]:
    """Validate a model answer. ``allowed_ids`` None means no-RAG (citations must be empty).

    Returns (fixes sorted by rank, dropped unknown citation ids); raises ValueError.
    """
    m = _FENCE.search(text)
    raw = m.group(1) if m else text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc})") from None
    fixes = data.get("fixes") if isinstance(data, dict) else None
    if not isinstance(fixes, list) or len(fixes) != N_FIXES:
        raise ValueError(f'expected an object with "fixes": a list of exactly {N_FIXES} items')
    out, dropped = [], []
    for f in fixes:
        if not isinstance(f, dict):
            raise ValueError("each fix must be an object")
        setting = str(f.get("setting") or "").strip()
        value = str(f.get("proposed_value") if f.get("proposed_value") is not None else "")
        reasoning = str(f.get("reasoning") or "").strip()
        if not _SETTING.match(setting):
            raise ValueError(f"setting {setting!r} is not a PostgreSQL parameter name")
        if not value.strip() or not reasoning:
            raise ValueError(f"fix for {setting}: proposed_value and reasoning must be non-empty")
        cited = f.get("cited_chunks") or []
        if not isinstance(cited, list):
            raise ValueError(f"fix for {setting}: cited_chunks must be a list")
        if allowed_ids is None:
            if cited:
                raise ValueError("cited_chunks must be empty: no reference documentation given")
        else:
            dropped += [str(c) for c in cited if str(c) not in allowed_ids]
            cited = [str(c) for c in cited if str(c) in allowed_ids]
        out.append(
            {
                "rank": f.get("rank"),
                "setting": setting,
                "proposed_value": value.strip(),
                "reasoning": reasoning,
                "cited_chunks": cited,
            }
        )
    if sorted(f["rank"] for f in out if isinstance(f["rank"], int)) != list(range(1, N_FIXES + 1)):
        raise ValueError(f"ranks must be the integers 1 to {N_FIXES}, each once")
    if len({f["setting"] for f in out}) != N_FIXES:
        raise ValueError("each fix must name a different setting")
    return sorted(out, key=lambda f: f["rank"]), dropped


def make_client():
    """OpenRouter (any model, chat API) when OPENROUTER_API_KEY is set, else Anthropic."""
    cfg = get_config()
    if cfg.llm_provider == "openrouter":
        from pgdiag.llm import OpenRouterClient

        return OpenRouterClient(
            cfg.openrouter_api_key,
            timeout=TIMEOUT_S,
            max_retries=SDK_RETRIES,
            provider_order=cfg.openrouter_provider,
        )
    if cfg.llm_provider == "anthropic":
        import anthropic

        return anthropic.Anthropic(
            api_key=cfg.anthropic_api_key, timeout=TIMEOUT_S, max_retries=SDK_RETRIES
        )
    raise DiagnosisError("set OPENROUTER_API_KEY or ANTHROPIC_API_KEY (.env or environment)")


def diagnose(
    symptoms: dict,
    mode: str,
    chunks: list | None = None,
    client=None,
    model: str | None = None,
) -> dict:
    """Ask the model for the top fixes; return the fixes plus a record of the exchange."""
    if mode not in ("rag", "norag"):
        raise ValueError(f"mode must be 'rag' or 'norag', not {mode!r}")
    chunk_dicts = [_chunk_dict(c) for c in chunks or []] if mode == "rag" else []
    if mode == "rag" and not chunk_dicts:
        raise ValueError("rag mode needs retrieved chunks")
    model = model or get_config().anthropic_model
    if not model:
        raise DiagnosisError("ANTHROPIC_MODEL is not set (.env or environment)")
    client = client or make_client()
    allowed = {c["chunk_id"] for c in chunk_dicts} if mode == "rag" else None

    prompt = build_prompt(symptoms, chunk_dicts)
    messages: list[dict] = [{"role": "user", "content": prompt}]
    responses: list[dict] = []
    errors: list[str] = []
    for _ in range(1 + MAX_REPAIRS):
        # No temperature: the API version this SDK sends (2026-10-21) rejects sampling
        # parameters on every model.
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM, messages=messages
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        responses.append(
            {
                "text": text,
                "stop_reason": resp.stop_reason,
                "model": getattr(resp, "model", model),
                "provider": getattr(resp, "provider", None),
                "usage": {
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            }
        )
        try:
            fixes, dropped = parse_fixes(text, allowed)
        except ValueError as exc:
            problem = str(exc)
            if resp.stop_reason == "max_tokens":
                problem = f"output cut off at the {MAX_TOKENS}-token limit ({exc})"
            errors.append(problem)
            hint = (
                " Think more briefly so the answer fits."
                if resp.stop_reason == "max_tokens"
                else ""
            )
            messages += [
                # the full content (thinking blocks included), as the API expects
                {"role": "assistant", "content": resp.content or "(empty answer)"},
                {
                    "role": "user",
                    "content": f"That answer was invalid: {problem}.{hint} Reply again with the "
                    "JSON object only, following the form and rules above.",
                },
            ]
            continue
        return {
            "mode": mode,
            "model": model,
            "fixes": fixes,
            "dropped_citations": dropped,
            "attempts": len(responses),
            "errors": errors,
            "prompt": {"system": SYSTEM, "user": prompt},
            "chunk_ids": [c["chunk_id"] for c in chunk_dicts],
            "responses": responses,
            "usage": {
                k: sum(r["usage"][k] for r in responses) for k in ("input_tokens", "output_tokens")
            },
        }
    raise DiagnosisError(f"no valid answer after {len(responses)} attempts: {errors}")
