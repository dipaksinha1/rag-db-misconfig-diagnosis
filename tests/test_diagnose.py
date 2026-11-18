"""T9 diagnosis: offline tests with a fake client, and one live check (marker ``llm``)."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from pgdiag.config import get_config
from pgdiag.diagnose.core import DOCS_HEADER, DiagnosisError, build_prompt, diagnose
from pgdiag.settings import CASE_SETTINGS, TARGET_SETTINGS

SYMPTOMS = {"database_stats": {"temp_files": 35000, "hit_pct": 97.0}}
CHUNKS = [
    {"chunk_id": "docs:a#X", "source": "docs", "title": "A", "url": "u", "text": "alpha"},
    {"chunk_id": "docs:b#Y", "source": "docs", "title": "B", "url": "u", "text": "beta"},
]


def answer(cites=(), **over) -> str:
    fixes = [
        {
            "rank": r,
            "setting": f"param_{r}",
            "proposed_value": "4MB",
            "reasoning": "because",
            "cited_chunks": list(cites),
        }
        for r in range(1, 5)
    ]
    fixes[0].update(over)
    return json.dumps({"fixes": fixes})


class FakeClient:
    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        text = self.texts.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            stop_reason="end_turn",
            model=kw["model"],
            usage=SimpleNamespace(input_tokens=100, output_tokens=10),
        )


def run(mode, *texts, chunks=None):
    client = FakeClient(*texts)
    return diagnose(SYMPTOMS, mode, chunks, client=client, model="m"), client


def test_valid_norag():
    rec, client = run("norag", answer())
    assert [f["rank"] for f in rec["fixes"]] == [1, 2, 3, 4]
    assert rec["fixes"][0]["setting"] == "param_1"
    assert rec["attempts"] == 1 and rec["usage"] == {"input_tokens": 100, "output_tokens": 10}
    assert rec["prompt"]["user"] == client.calls[0]["messages"][0]["content"]
    assert DOCS_HEADER not in rec["prompt"]["user"]


def test_valid_rag_with_fenced_json_and_citations():
    rec, client = run("rag", f"Here:\n```json\n{answer(['docs:a#X'])}\n```", chunks=CHUNKS)
    assert rec["fixes"][0]["cited_chunks"] == ["docs:a#X"]
    assert rec["chunk_ids"] == ["docs:a#X", "docs:b#Y"]
    assert "[docs:a#X] A\nalpha" in client.calls[0]["messages"][0]["content"]


def test_unknown_citation_dropped_and_recorded():
    rec, _ = run("rag", answer(["docs:a#X", "docs:nope"]), chunks=CHUNKS)
    assert rec["fixes"][0]["cited_chunks"] == ["docs:a#X"]
    assert set(rec["dropped_citations"]) == {"docs:nope"}


def test_invalid_then_retry_succeeds():
    rec, client = run("norag", "not json", answer(setting="Bad Name"), answer())
    assert rec["attempts"] == 3 and len(rec["errors"]) == 2
    last = client.calls[-1]["messages"]
    assert [m["role"] for m in last] == ["user", "assistant", "user", "assistant", "user"]
    assert "invalid" in last[-1]["content"]


@pytest.mark.parametrize(
    "bad",
    [
        json.dumps({"fixes": []}),
        answer(rank=2),  # duplicate rank
        answer(setting="param_2"),  # duplicate setting
        answer(proposed_value=""),
        answer(["docs:a#X"]),  # citations in no-RAG mode
    ],
)
def test_persistent_invalid_raises(bad):
    with pytest.raises(DiagnosisError):
        run("norag", bad, bad, bad)


def test_norag_prompt_is_rag_prompt_minus_docs_block():
    rag = build_prompt(SYMPTOMS, CHUNKS)
    norag = build_prompt(SYMPTOMS)
    block = DOCS_HEADER + "[docs:a#X] A\nalpha\n\n[docs:b#Y] B\nbeta\n\n"
    assert block in rag and rag.replace(block, "") == norag


def test_prompt_names_no_case_or_setting():
    prompt = build_prompt(SYMPTOMS).lower()
    assert not re.search(r"c\d\d_", prompt)
    for name in TARGET_SETTINGS:
        assert name not in prompt


@pytest.mark.llm
def test_live_norag_one_case():
    cfg = get_config()
    path = cfg.results_dir / "symptoms" / f"c02_{CASE_SETTINGS[1]}.json"
    if not cfg.anthropic_api_key or not cfg.anthropic_model:
        pytest.skip("ANTHROPIC_API_KEY / ANTHROPIC_MODEL not configured")
    if not path.exists():
        pytest.skip(f"{path} missing (run pgdiag-collect all)")
    rec = diagnose(json.loads(path.read_text()), "norag")
    assert len(rec["fixes"]) == 4
