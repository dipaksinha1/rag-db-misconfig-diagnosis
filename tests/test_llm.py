"""OpenRouter chat client: offline tests with a mocked HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from pgdiag.config import load_config
from pgdiag.diagnose import core as dcore
from pgdiag.diagnose.core import DiagnosisError
from pgdiag.llm import (
    RATE_LIMIT_RETRIES,
    LLMError,
    LLMFatalError,
    OpenRouterClient,
    TextBlock,
)

Q = [{"role": "user", "content": "q"}]


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


def client_with(*responses, **kw):
    seen, waits = [], []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        return queue.pop(0)

    c = OpenRouterClient(
        "sk-or-test", transport=httpx.MockTransport(handler), sleep=waits.append, **kw
    )
    return c, seen, waits


def test_request_and_response_mapping():
    c, seen, _ = client_with(ok('{"a": 1}'))
    resp = c.messages.create(
        model="openai/gpt-oss-120b",
        max_tokens=50,
        system="sys",
        messages=[
            {"role": "user", "content": "q"},
            # a previous answer, as the repair loop sends it back (list of blocks)
            {"role": "assistant", "content": [TextBlock("old")]},
            {"role": "user", "content": "again"},
        ],
    )
    req = seen[0]
    assert req.url.path == "/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-or-test"
    body = json.loads(req.content)
    assert body["model"] == "openai/gpt-oss-120b" and body["max_tokens"] == 50
    assert "provider" not in body
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


def test_provider_pinning():
    c, seen, _ = client_with(ok(), provider_order=["Crusoe", "Other"])
    c.messages.create(model="m", max_tokens=5, messages=Q)
    body = json.loads(seen[0].content)
    assert body["provider"] == {"order": ["Crusoe", "Other"], "allow_fallbacks": False}


def test_length_and_empty_answer():
    c, _, _ = client_with(ok("", finish="length"))
    resp = c.messages.create(model="m", max_tokens=5, messages=Q)
    assert resp.content == [] and resp.stop_reason == "max_tokens"


def test_retries_transient_then_succeeds():
    c, seen, waits = client_with(httpx.Response(502), httpx.Response(503), ok())
    resp = c.messages.create(model="m", max_tokens=5, messages=Q)
    assert len(seen) == 3 and resp.content[0].text == "hi"
    assert waits == [2.0, 4.0]


def test_rate_limit_honours_retry_after_and_has_own_budget():
    limited = [httpx.Response(429, headers={"retry-after": "30"})] * 5
    c, seen, waits = client_with(*limited, ok(), max_retries=1)
    c.messages.create(model="m", max_tokens=5, messages=Q)
    assert len(seen) == 6 and waits == [30.0] * 5  # more 429s than max_retries, still fine
    c, _, waits = client_with(httpx.Response(429), httpx.Response(429), ok())
    c.messages.create(model="m", max_tokens=5, messages=Q)
    assert waits == [5.0, 10.0]  # no header: growing wait


def test_rate_limit_gives_up_eventually():
    c, seen, _ = client_with(*[httpx.Response(429)] * (RATE_LIMIT_RETRIES + 1))
    with pytest.raises(LLMError, match="429"):
        c.messages.create(model="m", max_tokens=5, messages=Q)
    assert len(seen) == RATE_LIMIT_RETRIES + 1


def test_gives_up_after_retries():
    c, seen, _ = client_with(*[httpx.Response(503)] * 4)
    with pytest.raises(LLMError, match="503"):
        c.messages.create(model="m", max_tokens=5, messages=Q)
    assert len(seen) == 4


@pytest.mark.parametrize("status", [401, 402, 403])
def test_fatal_status_not_retried(status):
    c, seen, _ = client_with(httpx.Response(status, text="bad key"))
    with pytest.raises(LLMFatalError, match=str(status)):
        c.messages.create(model="m", max_tokens=5, messages=Q)
    assert len(seen) == 1


def test_other_client_error_not_retried():
    c, seen, _ = client_with(httpx.Response(400, text="bad model"))
    with pytest.raises(LLMError, match="400"):
        c.messages.create(model="m", max_tokens=5, messages=Q)
    assert len(seen) == 1


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(200, json={"error": {"message": "no provider"}}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, text="<html>gateway</html>"),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}, "finish_reason": "error"}]},
        ),
        httpx.Response(
            200,
            json={"choices": [{"message": {"content": ""}, "error": {"message": "host died"}}]},
        ),
    ],
)
def test_errors_inside_200_are_retried(bad):
    c, seen, _ = client_with(bad, ok("fine"))
    resp = c.messages.create(model="m", max_tokens=5, messages=Q)
    assert len(seen) == 2 and resp.content[0].text == "fine"


def test_errors_inside_200_give_up_as_llm_error():
    bad = httpx.Response(200, text="not json")
    c, _, _ = client_with(*[bad] * 4)
    with pytest.raises(LLMError, match="invalid JSON"):
        c.messages.create(model="m", max_tokens=5, messages=Q)


def test_llm_errors_are_diagnosis_errors():
    assert issubclass(LLMError, DiagnosisError) and issubclass(LLMFatalError, LLMError)


def test_make_client_picks_openrouter(monkeypatch, tmp_path):
    env = {
        "OPENROUTER_API_KEY": "sk-or-x",
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "OPENROUTER_PROVIDER": "Crusoe, Other",
    }
    monkeypatch.setattr(dcore, "get_config", lambda: load_config(env=env, root=tmp_path))
    c = dcore.make_client()
    assert isinstance(c, OpenRouterClient)
    assert c._http.headers["authorization"] == "Bearer sk-or-x"
    assert c.provider_order == ["Crusoe", "Other"]


def test_make_client_without_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(dcore, "get_config", lambda: load_config(env={}, root=tmp_path))
    with pytest.raises(DiagnosisError, match="OPENROUTER_API_KEY"):
        dcore.make_client()


def fixes_json():
    fixes = [
        {"rank": r, "setting": s, "proposed_value": "1", "reasoning": "b", "cited_chunks": []}
        for r, s in enumerate(["work_mem", "shared_buffers", "random_page_cost", "jit"], 1)
    ]
    return json.dumps({"fixes": fixes})


def test_diagnose_works_through_openrouter_client_and_records_provider():
    c, seen, _ = client_with(ok("not json"), ok(fixes_json()))
    rec = dcore.diagnose({"x": 1}, "norag", client=c, model="openai/gpt-oss-120b")
    assert rec["attempts"] == 2 and rec["fixes"][0]["setting"] == "work_mem"
    assert [r["provider"] for r in rec["responses"]] == ["SomeHost", "SomeHost"]
    repair = json.loads(seen[1].content)["messages"]
    assert repair[-2] == {"role": "assistant", "content": "not json"}


def test_diagnose_names_truncation_in_repair():
    c, seen, _ = client_with(ok("", finish="length"), ok(fixes_json()))
    rec = dcore.diagnose({"x": 1}, "norag", client=c, model="m")
    assert "token limit" in rec["errors"][0]
    repair = json.loads(seen[1].content)["messages"]
    assert repair[-2]["content"] == "(empty answer)"
    assert "Think more briefly" in repair[-1]["content"]
