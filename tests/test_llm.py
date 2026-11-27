"""OpenRouter chat client: offline tests with a mocked HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from pgdiag.diagnose import core as dcore
from pgdiag.llm import LLMError, OpenRouterClient


def ok(text="hi", finish="stop"):
    return httpx.Response(
        200,
        json={
            "model": "openai/gpt-oss-120b",
            "provider": "SomeHost",
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": finish}
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        },
    )


def client_with(*responses):
    seen = []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        return queue.pop(0)

    c = OpenRouterClient("sk-or-test", transport=httpx.MockTransport(handler), sleep=lambda s: None)
    return c, seen


def test_request_and_response_mapping():
    c, seen = client_with(ok('{"a": 1}'))
    resp = c.messages.create(
        model="openai/gpt-oss-120b",
        max_tokens=50,
        system="sys",
        messages=[
            {"role": "user", "content": "q"},
            # a previous answer, as the repair loop sends it back (list of blocks)
            {"role": "assistant", "content": resp_blocks("old")},
            {"role": "user", "content": "again"},
        ],
    )
    req = seen[0]
    assert req.url.path == "/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-or-test"
    body = json.loads(req.content)
    assert body["model"] == "openai/gpt-oss-120b" and body["max_tokens"] == 50
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "old"},
        {"role": "user", "content": "again"},
    ]
    assert [b.text for b in resp.content] == ['{"a": 1}']
    assert resp.stop_reason == "end_turn"
    assert resp.usage.input_tokens == 12 and resp.usage.output_tokens == 3
    assert resp.provider == "SomeHost"


def resp_blocks(text):
    from pgdiag.llm import TextBlock

    return [TextBlock(text)]


def test_length_and_empty_answer():
    c, _ = client_with(ok("", finish="length"))
    resp = c.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "q"}])
    assert resp.content == [] and resp.stop_reason == "max_tokens"


def test_retries_transient_then_succeeds():
    c, seen = client_with(httpx.Response(429, text="slow down"), httpx.Response(502), ok())
    resp = c.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "q"}])
    assert len(seen) == 3 and resp.content[0].text == "hi"


def test_gives_up_after_retries_and_on_client_errors():
    c, seen = client_with(*[httpx.Response(503)] * 4)
    with pytest.raises(LLMError, match="503"):
        c.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "q"}])
    assert len(seen) == 4
    c, seen = client_with(httpx.Response(401, text="bad key"))
    with pytest.raises(LLMError, match="401"):
        c.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "q"}])
    assert len(seen) == 1


def test_error_in_200_body():
    c, _ = client_with(httpx.Response(200, json={"error": {"message": "no provider"}}))
    with pytest.raises(LLMError, match="no provider"):
        c.messages.create(model="m", max_tokens=5, messages=[{"role": "user", "content": "q"}])


def test_diagnose_works_through_openrouter_client():
    fixes = [
        {
            "rank": r,
            "setting": s,
            "proposed_value": "1",
            "reasoning": "because",
            "cited_chunks": [],
        }
        for r, s in enumerate(["work_mem", "shared_buffers", "random_page_cost", "jit"], 1)
    ]
    c, seen = client_with(ok("not json"), ok(json.dumps({"fixes": fixes})))
    rec = dcore.diagnose({"x": 1}, "norag", client=c, model="openai/gpt-oss-120b")
    assert rec["attempts"] == 2 and rec["fixes"][0]["setting"] == "work_mem"
    repair = json.loads(seen[1].content)["messages"]
    assert repair[-2] == {"role": "assistant", "content": "not json"}
