"""pgdiag lab: a local web UI over the pipeline. Start it with ``make ui``.

1. Database     the ten target settings, running vs baseline
2. Break a knob set ONE setting to a bad value (apply_case: RESET ALL, ALTER SYSTEM, restart)
3. Symptoms     run the workload once and collect the symptom document (collect)
4. Ask Claude   RAG and/or no-RAG diagnosis: four ranked fixes each (retrieve + diagnose)
5. Test fixes   benchmark every suggestion on the broken server, one at a time, each undone
                and verified before the next, bracketed by two healthy runs (fixtest)
6. History      earlier sessions saved under results/ui/

The UI calls the same code as the CLI stages (``pgdiag.experiment.core.Steps``), so what it
measures is what the experiment measures. Only one session may touch the database at once.
"""

from __future__ import annotations

import json
import subprocess

import altair as alt
import pandas as pd
import streamlit as st

from pgdiag.config import get_config
from pgdiag.experiment.core import Steps
from pgdiag.faults import inject
from pgdiag.faults.cases import baseline_config_values, load_cases
from pgdiag.retrieve.core import DEFAULT_K
from pgdiag.settings import CASE_SETTINGS, TARGET_SETTINGS
from pgdiag.ui import logic

# Chart colors (dataviz reference palette): fixes in series-1 blue, references in muted gray.
FIX_COLOR = "#2a78d6"
REF_COLOR = "#898781"
BENCH_S = 45  # one "quick" benchmark (1 warm-up + 5 passes) on a 4-core host, roughly

st.set_page_config(page_title="pgdiag lab", page_icon=":material/tune:", layout="wide")
cfg = get_config()
LOCK = cfg.data_dir / "ui" / "db.lock"
S = st.session_state


# --- helpers --------------------------------------------------------------------


def srv() -> inject.PgServer:
    return inject.PgServer(cfg)


def show(name: str) -> str:
    with srv().connect() as conn:
        return conn.execute(f"SHOW {name}").fetchone()[0]  # name from TARGET_SETTINGS only


def db_ok() -> str | None:
    try:
        show("server_version")
        return None
    except Exception as exc:  # noqa: BLE001 - shown to the user
        return f"{type(exc).__name__}: {exc}"


def index_ready() -> bool:
    return (cfg.index_dir / "manifest.json").exists() and (cfg.index_dir / "chroma").exists()


def recorded_case():
    state = inject.load_state(inject.default_state_path(cfg))
    return logic.case_from_state(state.get("case"))


def clear_downstream(*keys: str) -> None:
    for k in keys:
        S.pop(k, None)


def oom_kills() -> int | None:
    """The container's cgroup OOM-kill count (None if it cannot be read)."""
    try:
        out = subprocess.run(
            ["docker", "exec", "pgdiag-db", "cat", "/sys/fs/cgroup/memory.events"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except Exception:  # noqa: BLE001 - only used for a hint
        return None
    return logic.oom_kill_count(out)


def run_op(title: str, fn):
    """Run ``fn(steps)`` under the DB lock with a live progress box; return its result."""
    kills_before = oom_kills()
    try:
        with logic.db_lock(LOCK), st.status(title, expanded=True) as box:
            steps = Steps(cfg, progress=lambda m: box.write(m))
            result = fn(steps)
            box.update(label=f"{title}: done", state="complete", expanded=False)
            return result
    except logic.Busy as exc:
        st.warning(str(exc))
    except Exception as exc:  # noqa: BLE001 - shown to the user
        st.error(f"{title} failed: {type(exc).__name__}: {exc}")
        kills_after = oom_kills()
        if None not in (kills_before, kills_after) and kills_after > kills_before:
            st.warning(
                "A database process was killed for using more memory than the container's "
                "limit (PGDIAG_DB_MEM_LIMIT, 256 MB by default). "
                "PostgreSQL restarted itself and the broken setting is still applied. "
                "This happens now and then on Docker Desktop when a bad setting makes queries "
                "write many temporary files; trying again usually works."
            )
    return None


def save(name: str, obj) -> None:
    """Write into this session's folder under results/ui/ (made on first use)."""
    if not S.get("session_dir"):
        case = S.get("case")
        if case is None:
            return
        S["session_dir"] = logic.new_session_dir(cfg.results_dir, case)
    logic.write_json(S["session_dir"] / name, obj)


def comparison_chart(rows: list[dict]) -> alt.Chart:
    data = pd.DataFrame([r for r in rows if r["median_s"] is not None])
    data["group"] = data["kind"].map(
        {"reference": "reference run", "fix": "suggested fix", "best fix": "suggested fix"}
    )
    data["note"] = data["kind"].map({"best fix": "  best"}).fillna("")
    data["value_label"] = data["median_s"].map(lambda v: f"{v:.2f} s") + data["note"]
    order = list(data["label"])
    y = alt.Y("label:N", sort=order, title=None, axis=alt.Axis(labelLimit=320))
    bars = (
        alt.Chart(data)
        .mark_bar(size=18, cornerRadiusEnd=4)
        .encode(
            x=alt.X("median_s:Q", title="workload time, median seconds (lower is better)"),
            y=y,
            color=alt.Color(
                "group:N",
                scale=alt.Scale(
                    domain=["reference run", "suggested fix"], range=[REF_COLOR, FIX_COLOR]
                ),
                legend=alt.Legend(title=None, orient="top"),
            ),
            tooltip=[
                alt.Tooltip("label:N", title="run"),
                alt.Tooltip("median_s:Q", title="median s", format=".2f"),
                alt.Tooltip("vs_healthy_pct:Q", title="vs healthy %", format="+.1f"),
                alt.Tooltip("recovery_pct:Q", title="recovered %", format=".0f"),
                alt.Tooltip("suggested_by:N", title="suggested by"),
                alt.Tooltip("status:N"),
            ],
        )
    )
    labels = (
        alt.Chart(data)
        .mark_text(align="left", dx=6, color=REF_COLOR)  # muted ink, readable in both themes
        .encode(x="median_s:Q", y=y, text="value_label:N")
    )
    return (bars + labels).properties(height=chart_height(len(data)))


def chart_height(n_bars: int) -> int:
    return 70 + 34 * n_bars  # 34 px per bar + room for the legend and the x axis


def results_view(fixtest: dict, suggestions: list[dict]) -> None:
    rows = logic.result_rows(fixtest, suggestions)
    best = fixtest.get("best")
    c1, c2, c3 = st.columns(3)
    c1.metric("Healthy", f"{fixtest['ref_s']:.2f} s")
    c2.metric(
        "Broken",
        f"{fixtest['broken']['median_s']:.2f} s",
        f"{fixtest['broken_vs_ref_pct']:+.1f}% vs healthy",
        delta_color="inverse",
    )
    if best:
        c3.metric(
            f"Fastest fix: {best['setting']}",
            best["proposed_value"],
            f"recovered {best['recovery_pct']:.0f}%" if best["recovery_pct"] is not None else None,
        )
    else:
        c3.metric("Fastest fix", "none applied")
    n_bars = sum(r["median_s"] is not None for r in rows)
    st.altair_chart(comparison_chart(rows), width="stretch", height=chart_height(n_bars))
    table = pd.DataFrame(rows).drop(columns=["kind"]).fillna("")
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "median_s": st.column_config.NumberColumn("median s", format="%.2f"),
            "vs_healthy_pct": st.column_config.NumberColumn("vs healthy %", format="%+.1f"),
            "recovery_pct": st.column_config.NumberColumn("recovered %", format="%.0f"),
        },
    )
    st.caption(
        "Recovered % = how much of the slowdown the fix undid (100% = back to healthy). "
        "'Within 10%' = the fix brings the workload to within 10% of the healthy time. "
        "Healthy is the mean of two baseline runs, one before and one after the fixes."
    )


# --- sidebar ----------------------------------------------------------------------

problem = db_ok()
if "case" not in S:
    S["case"] = recorded_case() if not problem else None

with st.sidebar:
    st.title("pgdiag lab")
    st.caption("Break a PostgreSQL setting, ask Claude, test its fixes.")
    if problem:
        st.error("Test database unreachable. Start Docker Desktop, then run `make db-up`.")
        st.caption(problem)
    else:
        st.success("Test database: running")
    case = S.get("case")
    if case:
        st.warning(f"Broken: **{case.setting} = {case.bad_value}**")
    else:
        st.info("Configuration: baseline (healthy)")
    st.write(
        f"API key: {cfg.llm_provider or '**missing**'}  \n"
        f"Model: `{cfg.anthropic_model or 'not set'}`  \n"
        f"RAG index: {'ready' if index_ready() else '**not built**'}"
    )
    if st.button("Reset database to baseline", disabled=bool(problem), width="stretch"):
        if run_op("Reset to baseline", lambda s: s.reset() or True):
            S["case"] = None
            clear_downstream("session_dir", "symptoms", "diagnoses", "fixtest", "suggestions")
            st.rerun()
    st.divider()
    st.caption(
        "Only one action runs at a time. Changing `.env` (API key, model) needs a restart "
        "of `make ui`."
    )

if problem:
    st.stop()

tabs = st.tabs(
    [
        "1 · Database",
        "2 · Break a knob",
        "3 · Symptoms",
        "4 · Ask Claude",
        "5 · Test fixes",
        "History",
    ]
)

# --- 1. database ---------------------------------------------------------------------

with tabs[0]:
    st.subheader("The ten settings this project studies")
    st.caption(
        "Running = what the server uses now. Baseline = the healthy value in "
        "`docker/postgres/postgresql.conf`. The six marked 'experiment' are the ones the "
        "experiment breaks."
    )
    baseline = baseline_config_values(cfg)
    server = srv()
    auto = server.auto_conf()
    rows = []
    for name in TARGET_SETTINGS:
        running = show(name)
        info = server.info(name)
        rows.append(
            {
                "setting": name,
                "running": running,
                "baseline": baseline[name],
                "changed": "yes" if name in auto else "",
                "experiment": "yes" if name in CASE_SETTINGS else "",
                "restart needed": "yes" if info.restart_required else "",
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if auto:
        st.write("ALTER SYSTEM overrides in `postgresql.auto.conf`:", auto)
    st.button("Refresh")

# --- 2. break a knob -----------------------------------------------------------------

with tabs[1]:
    st.subheader("Set one setting to a bad value")
    st.caption(
        "This runs `ALTER SYSTEM RESET ALL`, then `ALTER SYSTEM SET <setting> = <value>`, "
        "restarts the server and checks the other nine settings are at baseline. One "
        "setting at a time, as in the experiment."
    )
    cases = {c.setting: c for c in load_cases()}
    baseline = baseline_config_values(cfg)
    setting = st.selectbox(
        "Setting",
        list(TARGET_SETTINGS),
        format_func=lambda n: f"{n}  (experiment case)" if n in cases else n,
    )
    info = srv().info(setting)
    c1, c2, c3 = st.columns(3)
    c1.metric("Healthy value", baseline[setting])
    c2.metric("Running now", show(setting))
    c3.metric("Needs a restart", "yes" if info.restart_required else "no (reload is enough)")
    if info.min_val is not None:
        unit = f" (unit: {info.unit})" if info.unit else ""
        st.caption(f"Allowed by PostgreSQL: {info.min_val} to {info.max_val}{unit}")
    elif info.enumvals:
        st.caption("Allowed values: " + ", ".join(info.enumvals))
    if setting in cases:
        st.info(
            f"The experiment breaks this one with **{cases[setting].bad_value}**.  \n"
            f"{cases[setting].rationale}"
        )
    default = cases[setting].bad_value if setting in cases else baseline[setting]
    value = st.text_input(
        "Bad value to set",
        value=default,
        key=f"value_{setting}",
        help="Units as in postgresql.conf, e.g. 64kB, 1MB, 100, off.",
    )
    if st.button("Apply and restart", type="primary"):
        case = logic.case_for(setting, value, baseline[setting], info.restart_required)

        def apply(steps: Steps):
            steps.apply_case(case)
            return True

        if run_op(f"Break {setting} = {value}", apply):
            S["case"] = case
            S["session_dir"] = logic.new_session_dir(cfg.results_dir, case)
            clear_downstream("symptoms", "diagnoses", "fixtest", "suggestions")
            st.success(f"{setting} is now {show(setting)}. Next: tab 3, Symptoms.")
            st.rerun()

# --- 3. symptoms -----------------------------------------------------------------------

with tabs[2]:
    st.subheader("Run the workload and collect what a DBA would look at")
    case = S.get("case")
    if not case:
        st.info("Break a setting first (tab 2).")
    else:
        st.caption(
            "Runs the 8 workload queries and pgbench once (about a minute), then reads "
            "pg_stat_statements, I/O and table statistics, the server log and EXPLAIN "
            "plans of the slowest queries. This document is all Claude gets to see; it never "
            "names the broken setting."
        )
        if st.button("Collect symptoms", type="primary"):

            def collect(steps: Steps):
                doc = steps.collect()
                leaks = steps.leak_problems(doc)
                if leaks:
                    raise RuntimeError(f"symptom document leaks the answer: {leaks[:3]}")
                return doc

            doc = run_op("Collect symptoms", collect)
            if doc:
                S["symptoms"] = doc
                clear_downstream("diagnoses", "fixtest", "suggestions")
                save("symptoms.json", doc)
        doc = S.get("symptoms")
        if doc:
            t = doc["workload_timing"]
            st.metric("Workload time (one pass)", f"{t['total_s']:.2f} s")
            st.write("**Time per workload component (seconds)**")
            st.dataframe(
                pd.DataFrame(
                    [{"component": k, "seconds": v} for k, v in t["components_s"].items()]
                ),
                hide_index=True,
            )
            st.write("**Top statements (pg_stat_statements)**")
            st.dataframe(pd.DataFrame(doc["top_statements"]), hide_index=True)
            with st.expander("Server log summary"):
                st.json(doc["log_summary"])
            with st.expander("EXPLAIN plans of the slowest queries"):
                for e in doc.get("explain", []):
                    st.code(e.get("plan", ""), language="text")
            with st.expander("Full symptom document (JSON)"):
                st.json(doc, expanded=False)

# --- 4. ask claude -----------------------------------------------------------------------

with tabs[3]:
    st.subheader("Ask Claude which setting is wrong")
    doc = S.get("symptoms")
    if not doc:
        st.info("Collect symptoms first (tab 3).")
    elif not (cfg.llm_provider and cfg.anthropic_model):
        st.warning(
            "Add `OPENROUTER_API_KEY=...` (and check `ANTHROPIC_MODEL`) in the `.env` file in "
            "the project folder, then stop and restart `make ui`."
        )
    else:
        rag_ok = index_ready()
        c1, c2 = st.columns(2)
        want_rag = c1.checkbox(
            "With RAG (docs retrieved from the index)",
            value=rag_ok,
            disabled=not rag_ok,
            help=None
            if rag_ok
            else "Build the index first: make ingest-docs, make ingest-release-notes, make index.",
        )
        want_norag = c2.checkbox("Without RAG (symptoms only)", value=True)
        if not rag_ok:
            st.caption("RAG is off until the index is built (`make index`).")
        st.caption("Each mode is one paid API call (sometimes two, if the answer needs a repair).")
        modes = [m for m, on in (("rag", want_rag), ("norag", want_norag)) if on]
        if st.button("Ask Claude", type="primary", disabled=not modes):

            def ask(steps: Steps):
                out = {}
                for mode in modes:
                    chunks = None
                    if mode == "rag":
                        steps.say(f"retrieving the top {DEFAULT_K} docs chunks")
                        chunks = steps.retrieve(doc, DEFAULT_K)
                    steps.say(f"asking {steps.model()} ({mode})")
                    record = steps.diagnose(doc, mode, chunks)
                    if chunks:
                        record["retrieved"] = chunks
                    out[mode] = record
                return out

            result = run_op("Ask Claude", ask)
            if result:
                S["diagnoses"] = result
                clear_downstream("fixtest", "suggestions")
                for mode, record in result.items():
                    save(f"diagnosis.{mode}.json", record)
        diagnoses = S.get("diagnoses") or {}
        cols = st.columns(max(1, len(diagnoses)))
        for col, (mode, record) in zip(cols, diagnoses.items(), strict=False):
            with col:
                st.markdown(f"#### {'With RAG' if mode == 'rag' else 'Without RAG'}")
                for f in record["fixes"]:
                    st.markdown(f"**{f['rank']}. `{f['setting']} = {f['proposed_value']}`**")
                    st.write(f["reasoning"])
                    if f.get("cited_chunks"):
                        st.caption("cites: " + ", ".join(f["cited_chunks"]))
                u = record["usage"]
                st.caption(
                    f"{record['model']} · {u['input_tokens']} tokens in, "
                    f"{u['output_tokens']} out · {record['attempts']} attempt(s)"
                )
                if record.get("retrieved"):
                    with st.expander(f"The {len(record['retrieved'])} docs chunks it was given"):
                        for c in record["retrieved"]:
                            st.markdown(f"**{c['chunk_id']}** · [{c['title']}]({c['url']})")
                            st.caption(c["text"][:600] + ("…" if len(c["text"]) > 600 else ""))

# --- 5. test fixes -------------------------------------------------------------------------

with tabs[4]:
    st.subheader("Try every suggestion on the broken database and time it")
    case = S.get("case")
    diagnoses = S.get("diagnoses") or {}
    manual = S.setdefault("manual_fixes", [])
    if not case and not S.get("fixtest"):
        st.info("Break a setting first (tab 2).")
    if case:
        st.caption(
            "Order of the test: reset → benchmark (healthy) → break → benchmark (broken) → "
            "for each fix: apply on top of the broken server → benchmark → undo → verify the "
            "server is exactly broken again → … → reset → benchmark (healthy). So every fix "
            "is measured alone, from the same starting point."
        )
        with st.form("add_fix", clear_on_submit=True):
            st.write("Add your own fix to compare (optional)")
            c1, c2, c3 = st.columns([2, 2, 1])
            m_setting = c1.text_input("Setting", placeholder="work_mem")
            m_value = c2.text_input("Value", placeholder="8MB")
            if c3.form_submit_button("Add") and m_setting and m_value:
                manual.append({"setting": m_setting, "proposed_value": m_value, "source": "you"})
        control = st.checkbox(
            f"Also test the known-good value ({case.setting} = {case.good_value}) as a control",
            value=True,
        )
        extra = list(manual)
        if control:
            extra.append(
                {"setting": case.setting, "proposed_value": case.good_value, "source": "control"}
            )
        key = Steps(cfg).fix_key
        suggestions = logic.merge_suggestions(diagnoses, extra, key=key)
        if not suggestions:
            st.info("No fixes yet: ask Claude (tab 4) or add your own above.")
        else:
            table = pd.DataFrame(
                [
                    {
                        "test": True,
                        "rank": s["rank"],
                        "setting": s["setting"],
                        "value": s["proposed_value"],
                        "suggested by": ", ".join(s["sources"]),
                    }
                    for s in suggestions
                ]
            )
            edited = st.data_editor(
                table,
                hide_index=True,
                width="stretch",
                disabled=["rank", "setting", "value", "suggested by"],
            )
            chosen = [s for s, keep in zip(suggestions, edited["test"], strict=True) if keep]
            minutes = (3 + len(chosen)) * BENCH_S / 60
            st.caption(f"{len(chosen)} fixes · about {minutes:.0f} minutes. Keep this tab open.")
            if st.button("Run the tests", type="primary", disabled=not chosen):

                def test(steps: Steps):
                    fixes = [
                        {
                            "rank": s["rank"],
                            "setting": s["setting"],
                            "proposed_value": s["proposed_value"],
                        }
                        for s in chosen
                    ]
                    return steps.test_fixes(case, fixes, "quick")

                out = run_op("Test fixes", test)
                if out:
                    S["fixtest"], S["suggestions"] = out, chosen
                    save("fixtest.json", out)
                    save("suggestions.json", chosen)
                # test_fixes always ends at the baseline (also when it fails)
                S["case"] = recorded_case()
                st.rerun()
    if S.get("fixtest"):
        st.divider()
        st.markdown("#### Results")
        st.caption("The database is back at its healthy baseline after the test.")
        results_view(S["fixtest"], S["suggestions"])

# --- history ---------------------------------------------------------------------------------

with tabs[5]:
    st.subheader("Earlier sessions")
    sessions = logic.list_sessions(cfg.results_dir)
    if not sessions:
        st.info("Nothing yet. Each session that reaches tab 5 is saved under `results/ui/`.")
    else:
        summary = []
        for s in sessions:
            ft, best = s["fixtest"], s["fixtest"].get("best") or {}
            summary.append(
                {
                    "session": s["name"],
                    "broken": f"{ft['setting']} = {ft['bad_value']}",
                    "slowdown %": ft["broken_vs_ref_pct"],
                    "fixes tested": len(ft["fixes"]),
                    "best fix": f"{best['setting']} = {best['proposed_value']}" if best else "-",
                    "best recovered %": best.get("recovery_pct"),
                }
            )
        st.dataframe(pd.DataFrame(summary), hide_index=True, width="stretch")
        pick = st.selectbox("Open a session", [s["name"] for s in sessions])
        chosen_s = next(s for s in sessions if s["name"] == pick)
        results_view(chosen_s["fixtest"], chosen_s["suggestions"])
        for mode in logic.MODES:
            path = chosen_s["dir"] / f"diagnosis.{mode}.json"
            if path.exists():
                with st.expander(f"Claude's answer ({mode})"):
                    for f in json.loads(path.read_text())["fixes"]:
                        st.markdown(f"**{f['rank']}. `{f['setting']} = {f['proposed_value']}`**")
                        st.write(f["reasoning"])
