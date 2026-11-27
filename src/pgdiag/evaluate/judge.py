"""Blinded reasoning judge (T12): a model grades each diagnosis's reasoning against the truth.

For every row of ``results.jsonl`` the judge gets the symptom report the diagnosing model saw,
the true cause (broken setting, its bad value, the healthy value) and the 4 fixes with their
reasoning. It does **not** get the mode, the retrieved documentation, the citations or the
measured outcomes. Chunk ids quoted inside the reasoning are replaced with ``[reference]`` so
they do not reveal the mode (wording like "the documentation says" can still leak it; see the
report's caveats). Each answer is graded on its own (no pairwise comparison), so the order in
which RAG and no-RAG answers are seen does not matter.

Scores (integers 1-5): ``evidence`` (cites real, specific signals from the report),
``accuracy`` (claims about PostgreSQL and about the report are correct), ``diagnosis``
(identifies the actual cause and explains the mechanism), ``overall``; plus
``unsupported_claims`` (count of claims contradicted by or absent from the report) and a short
``comment``. Results are cached one file per run in ``<out>/judge/``, so the step is resumable.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Callable
from pathlib import Path

from pgdiag.config import get_config
from pgdiag.faults.cases import get_case

CRITERIA = ("evidence", "accuracy", "diagnosis", "overall")
MAX_TOKENS = 8000  # as in T9: reasoning models may think long
MAX_REPAIRS = 2
PROMPT_VERSION = 1

SYSTEM = (
    "You are a senior PostgreSQL 16 performance engineer grading another engineer's written "
    "diagnosis. You are strict, specific and consistent."
)

TEMPLATE = """\
An engineer was given the symptom report below from a slow PostgreSQL 16 server and asked for \
the 4 configuration parameters most likely to be misconfigured, ranked, with a value and a \
short reasoning for each. Grade the quality of the REASONING.

The true cause, which the engineer did NOT know: the parameter `{setting}` was set to \
`{bad_value}`; the healthy value is `{good_value}`. All other parameters were at their healthy \
values.

Symptom report the engineer saw (JSON):
{symptoms}

The engineer's answer:
{answer}

Score each criterion with an integer from 1 (very poor) to 5 (excellent):
- "evidence": the reasoning points to specific signals that really are in the report \
(numbers, plan nodes, log lines, statistics) and they support the fix.
- "accuracy": statements about PostgreSQL behaviour and about the report are correct; \
nothing is invented or misread.
- "diagnosis": the answer identifies the true cause (by name, at a high rank) and explains the \
mechanism linking it to the observed symptoms. 1 if the true cause is not proposed at all.
- "overall": your overall judgement of the reasoning as a diagnosis a DBA could act on.
Also count "unsupported_claims": statements about the report that the report contradicts or \
does not contain.

Answer with JSON only, in exactly this form:
{{"evidence": 1, "accuracy": 1, "diagnosis": 1, "overall": 1, "unsupported_claims": 0, \
"comment": "<at most two sentences>"}}
"""

_CHUNK_REF = re.compile(r"\[?\b(?:docs|relnotes|ml):[^\s,;\])]+\]?")
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class JudgeError(RuntimeError):
    pass


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def scrub(text: str) -> str:
    """Remove chunk ids (they would reveal the RAG mode)."""
    return _CHUNK_REF.sub("[reference]", text)


def format_answer(fixes: list[dict]) -> str:
    return "\n".join(
        f"{f['rank']}. {f['setting']} = {f['proposed_value']}\n   {scrub(f['reasoning'])}"
        for f in sorted(fixes, key=lambda f: f["rank"])
    )


def build_prompt(case_id: str, symptoms: dict, fixes: list[dict]) -> str:
    case = get_case(case_id)
    return TEMPLATE.format(
        setting=case.setting,
        bad_value=case.bad_value,
        good_value=case.good_value,
        symptoms=json.dumps(symptoms, indent=1),
        answer=format_answer(fixes),
    )


def parse_scores(text: str) -> dict:
    m = _FENCE.search(text)
    raw = m.group(1) if m else text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    out = {}
    for k in CRITERIA:
        v = data.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 5:
            raise ValueError(f'"{k}" must be an integer from 1 to 5')
        out[k] = v
    u = data.get("unsupported_claims")
    if not isinstance(u, int) or isinstance(u, bool) or u < 0:
        raise ValueError('"unsupported_claims" must be a non-negative integer')
    out["unsupported_claims"] = u
    out["comment"] = str(data.get("comment") or "").strip()
    return out


def judge_one(prompt: str, client, model: str) -> dict:
    """Call the judge, validating and repairing like T9; returns scores + exchange record."""
    messages: list[dict] = [{"role": "user", "content": prompt}]
    responses, errors = [], []
    for _ in range(1 + MAX_REPAIRS):
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM, messages=messages
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        responses.append(
            {
                "text": text,
                "stop_reason": resp.stop_reason,
                "provider": getattr(resp, "provider", None),
                "usage": {
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            }
        )
        try:
            scores = parse_scores(text)
        except ValueError as exc:
            errors.append(str(exc))
            messages += [
                {"role": "assistant", "content": resp.content or "(empty answer)"},
                {
                    "role": "user",
                    "content": f"That answer was invalid: {exc}. Reply again with the JSON "
                    "object only, in the form given above.",
                },
            ]
            continue
        return {
            "scores": scores,
            "attempts": len(responses),
            "errors": errors,
            "responses": responses,
        }
    raise JudgeError(f"no valid grade after {len(responses)} attempts: {errors}")


def judge_path(judge_dir: Path, row: dict) -> Path:
    return judge_dir / f"{row['case_id']}.r{row['repeat']}.{row['mode']}.json"


def load_judgements(judge_dir: Path | None) -> dict[tuple[str, int, str], dict]:
    """(case_id, repeat, mode) -> scores, for every cached judgement."""
    if not judge_dir or not judge_dir.exists():
        return {}
    out = {}
    for p in judge_dir.glob("*.json"):
        d = json.loads(p.read_text())
        out[(d["case_id"], d["repeat"], d["mode"])] = {
            **d["scores"],
            "judge_model": d["judge_model"],
        }
    return out


def run_judge(
    exp_dir: Path,
    judge_dir: Path,
    *,
    rows: list[dict],
    client=None,
    model: str | None = None,
    limit: int | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Grade every row without a cached judgement (at most ``limit``)."""
    say = progress or (lambda m: None)
    model = model or get_config().judge_model
    if not model:
        raise JudgeError("no judge model: set PGDIAG_JUDGE_MODEL or ANTHROPIC_MODEL")
    todo = [r for r in rows if not judge_path(judge_dir, r).exists()]
    cached = len(rows) - len(todo)
    if limit is not None:
        todo = todo[:limit]
    if todo and client is None:
        from pgdiag.diagnose.core import make_client

        client = make_client()
    judge_dir.mkdir(parents=True, exist_ok=True)
    done, failed = 0, []
    for i, r in enumerate(todo, start=1):
        label = f"{r['case_id']} r{r['repeat']} {r['mode']}"
        say(f"judge {i}/{len(todo)}: {label}")
        symptoms = json.loads((exp_dir / r["symptoms"]).read_text())
        diagnosis = json.loads((exp_dir / r["diagnosis"]).read_text())
        prompt = build_prompt(r["case_id"], symptoms, diagnosis["fixes"])
        try:
            res = judge_one(prompt, client, model)
        except JudgeError as exc:
            say(f"judge {label} failed: {exc}")
            failed.append(label)
            continue
        record = {
            "case_id": r["case_id"],
            "repeat": r["repeat"],
            "mode": r["mode"],
            "judge_model": model,
            "prompt_version": PROMPT_VERSION,
            "at": _now(),
            **res,
            "prompt": {"system": SYSTEM, "user": prompt},
        }
        path = judge_path(judge_dir, r)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record, indent=1) + "\n")
        tmp.replace(path)
        done += 1
    return {"graded": done, "failed": failed, "cached": cached}
