"""Offline tests for fault injection (T6): cases file, units, state file, set/undo logic.

No database: the server is a fake that models the baseline file, the ALTER SYSTEM
layer (postgresql.auto.conf) and reload/restart semantics.
"""

from __future__ import annotations

import json

import psycopg
import pytest
import yaml

from pgdiag.config import get_config
from pgdiag.faults import cases as cases_mod
from pgdiag.faults import inject, measure, units
from pgdiag.faults.cases import (
    CaseError,
    FaultCase,
    baseline_config_values,
    get_case,
    load_cases,
    parse_cases,
    parse_conf,
    validate_against_server,
    validate_cases,
)
from pgdiag.faults.inject import InvalidFix
from pgdiag.faults.units import InvalidValue, SettingInfo
from pgdiag.settings import CASE_SETTINGS, TARGET_SETTINGS
from pgdiag.workload.queryset import COMPONENT_IDS

# pg_settings rows of PostgreSQL 16.16 (vartype, unit, min, max, context), checked against the
# server by tests/test_faults_db.py::test_offline_setting_infos_match_the_server
INFOS = {
    "shared_buffers": SettingInfo(
        "shared_buffers", "integer", "8kB", "16", "1073741823", context="postmaster"
    ),
    "work_mem": SettingInfo("work_mem", "integer", "kB", "64", "2147483647", context="user"),
    "effective_cache_size": SettingInfo(
        "effective_cache_size", "integer", "8kB", "1", "2147483647", context="user"
    ),
    "random_page_cost": SettingInfo(
        "random_page_cost", "real", None, "0", "1.79769e+308", context="user"
    ),
    "max_parallel_workers_per_gather": SettingInfo(
        "max_parallel_workers_per_gather", "integer", None, "0", "1024", context="user"
    ),
    "autovacuum": SettingInfo("autovacuum", "bool", context="sighup"),
    "max_wal_size": SettingInfo(
        "max_wal_size", "integer", "MB", "2", "2147483647", context="sighup"
    ),
    "default_statistics_target": SettingInfo(
        "default_statistics_target", "integer", None, "1", "10000", context="user"
    ),
    "jit_above_cost": SettingInfo(
        "jit_above_cost", "real", None, "-1", "1.79769e+308", context="user"
    ),
    "maintenance_work_mem": SettingInfo(
        "maintenance_work_mem", "integer", "kB", "1024", "2147483647", context="user"
    ),
    "checkpoint_timeout": SettingInfo(
        "checkpoint_timeout", "integer", "s", "30", "86400", context="sighup"
    ),
    "wal_level": SettingInfo(
        "wal_level", "enum", enumvals=("minimal", "replica", "logical"), context="postmaster"
    ),
    "jit": SettingInfo("jit", "bool", context="user"),
    "port": SettingInfo("port", "integer", None, "1", "65535", context="postmaster"),
    "server_version_num": SettingInfo("server_version_num", "integer", context="internal"),
}


# ---------------------------------------------------------------------------
# units


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("shared_buffers", "256MB", 32768.0),
        ("shared_buffers", "1MB", 128.0),
        ("shared_buffers", "128", 128.0),  # no unit: the setting's own unit (8kB pages)
        ("shared_buffers", "1024kB", 128.0),
        ("shared_buffers", "0.25GB", 32768.0),
        ("work_mem", "64kB", 64.0),
        ("work_mem", "32MB", 32768.0),
        ("work_mem", " '32MB' ", 32768.0),
        ("effective_cache_size", "768MB", 98304.0),
        ("max_wal_size", "4GB", 4096.0),
        ("max_wal_size", "32MB", 32.0),
        ("maintenance_work_mem", "1MB", 1024.0),
        ("random_page_cost", "1.1", 1.1),
        ("random_page_cost", "40", 40.0),
        ("jit_above_cost", "1e5", 100000.0),
        ("checkpoint_timeout", "30min", 1800.0),
        ("checkpoint_timeout", "1h", 3600.0),
        ("checkpoint_timeout", "90", 90.0),
        ("autovacuum", "off", "off"),
        ("autovacuum", "TRUE", "on"),
        ("autovacuum", "0", "off"),
        ("autovacuum", "y", "on"),
        ("autovacuum", "of", "off"),
        ("wal_level", "Replica", "replica"),
    ],
)
def test_normalize(name, value, expected):
    assert units.values_equal(units.normalize(value, INFOS[name]), expected)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("work_mem", "32mb"),  # units are case-sensitive in PostgreSQL
        ("work_mem", "32 GiB"),
        ("work_mem", "lots"),
        ("random_page_cost", "4kB"),  # unitless setting
        ("checkpoint_timeout", "5MB"),  # memory unit on a time setting
        ("autovacuum", "maybe"),
        ("autovacuum", "o"),  # ambiguous prefix
        ("wal_level", "archive"),
    ],
)
def test_normalize_rejects(name, value):
    with pytest.raises(InvalidValue):
        units.normalize(value, INFOS[name])


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("work_mem", "32kB"),  # below the 64kB minimum
        ("maintenance_work_mem", "512kB"),
        ("max_wal_size", "1MB"),
        ("default_statistics_target", "0"),
        ("default_statistics_target", "20000"),
        ("random_page_cost", "-1"),
    ],
)
def test_validate_checks_range(name, value):
    with pytest.raises(InvalidValue, match="outside the allowed range"):
        units.validate(value, INFOS[name])


def test_same_value_and_bytes():
    sb = INFOS["shared_buffers"]
    assert units.same_value("1MB", "128", sb)
    assert units.same_value("256MB", "32768", sb)
    assert not units.same_value("1MB", "2MB", sb)
    assert units.to_bytes(units.normalize("256MB", sb), sb) == 256 * 2**20
    assert units.to_bytes(40.0, INFOS["random_page_cost"]) is None
    assert sb.restart_required and not INFOS["work_mem"].restart_required


def test_normalize_case_bad_values():
    assert units.normalize("1MB", INFOS["shared_buffers"]) == 128.0
    assert units.normalize("64kB", INFOS["work_mem"]) == 64.0
    assert units.normalize("0", INFOS["jit_above_cost"]) == 0.0
    assert units.same_value("8MB", "8192", INFOS["work_mem"])
    assert units.same_value("64MB", "8192", INFOS["shared_buffers"])


# ---------------------------------------------------------------------------
# the cases file


def test_cases_file_has_one_case_per_case_setting():
    cases = load_cases()
    assert len(cases) == 6
    assert [c.setting for c in cases] == list(CASE_SETTINGS)
    assert len({c.case_id for c in cases}) == 6
    for n, c in enumerate(cases, 1):
        assert c.case_id == f"c{n:02d}_{c.setting}"
        assert c.primary_component in c.symptom_components
        assert set(c.symptom_components) <= set(COMPONENT_IDS)
        assert len(c.rationale) > 40


def test_good_values_equal_the_baseline_config_file():
    """The known-correct fix of each case is exactly the healthy baseline value."""
    base = baseline_config_values()
    assert set(base) == set(TARGET_SETTINGS)
    for c in load_cases():
        assert c.good_value == base[c.setting], c.case_id
    assert validate_cases() == []


def test_bad_values_are_the_documented_choices():
    bad = {c.setting: c.bad_value for c in load_cases()}
    assert bad == {
        "shared_buffers": "1MB",
        "work_mem": "64kB",
        "random_page_cost": "100",
        "max_parallel_workers_per_gather": "0",
        "jit_above_cost": "0",
        "default_statistics_target": "1",
    }


def test_restart_and_analyze_flags():
    flags = {c.setting: (c.restart_required, c.needs_analyze) for c in load_cases()}
    assert [s for s, (r, _) in flags.items() if r] == ["shared_buffers"]
    assert [s for s, (_, a) in flags.items() if a] == ["default_statistics_target"]


def test_cases_validate_against_pg16_settings():
    """Bad and good values parse with each setting's unit and lie inside min/max."""
    assert validate_against_server(INFOS) == []
    for c in load_cases():
        bad = units.validate(c.bad_value, INFOS[c.setting])
        good = units.validate(c.good_value, INFOS[c.setting])
        assert not units.values_equal(bad, good)


def test_validate_against_server_reports_problems():
    infos = dict(INFOS)
    infos["work_mem"] = SettingInfo("work_mem", "integer", "kB", "128", "2147483647")
    infos["random_page_cost"] = SettingInfo("random_page_cost", "real", context="postmaster")
    del infos["jit_above_cost"]
    problems = validate_against_server(infos)
    assert any("c02_work_mem" in p and "outside" in p for p in problems)
    assert any("c03_random_page_cost" in p and "restart_required" in p for p in problems)
    assert any("c05_jit_above_cost" in p and "not in pg_settings" in p for p in problems)


def _raw_cases() -> dict:
    return yaml.safe_load(cases_mod.read_cases_text())


def test_parse_cases_rejects_bad_files():
    assert len(parse_cases(_raw_cases())) == 6

    def broken(mutate) -> dict:
        data = _raw_cases()
        mutate(data)
        return data

    bad_inputs = [
        broken(lambda d: d["cases"].pop()),  # missing a setting
        broken(lambda d: d["cases"].append(dict(d["cases"][0]))),  # 7 cases
        broken(lambda d: d["cases"][0].pop("good_value")),
        broken(lambda d: d["cases"][1].update(case_id="c03_work_mem")),
        broken(lambda d: d["cases"][1].update(restart_required="no")),
        broken(lambda d: d["cases"][1].update(primary_component="nope")),
        broken(lambda d: d["cases"][1].update(primary_component="pgbench")),
        broken(lambda d: d["cases"][1].update(bad_value="8MB")),
        broken(lambda d: d["cases"][1].update(rationale="")),
        broken(lambda d: d["cases"].reverse()),
        {"cases": "x"},
    ]
    for data in bad_inputs:
        with pytest.raises(CaseError):
            parse_cases(data)


def test_yaml_scalars_become_text():
    data = _raw_cases()
    data["cases"][3]["bad_value"] = 0  # an unquoted 0 in YAML
    data["cases"][2]["good_value"] = 1.1
    cases = parse_cases(data)
    assert cases[3].bad_value == "0"
    assert cases[2].good_value == "1.1"


def test_get_case_lookup():
    assert get_case("c02_work_mem").setting == "work_mem"
    assert get_case("2").case_id == "c02_work_mem"
    assert get_case("c06").setting == "default_statistics_target"
    assert get_case("jit_above_cost").case_id == "c05_jit_above_cost"
    with pytest.raises(KeyError):
        get_case("c07")
    with pytest.raises(KeyError):
        get_case("autovacuum")  # a target setting, but not a case


def test_parse_conf():
    text = """
# comment
shared_buffers = 64MB                     # restart required
work_mem=32MB
log_line_prefix = '%m [%p] # not a comment '
listen_addresses '*'
autovacuum = on
#jit_above_cost = 5
work_mem = 8MB
"""
    conf = parse_conf(text)
    assert conf["shared_buffers"] == "64MB"
    assert conf["work_mem"] == "8MB"  # last one wins
    assert conf["log_line_prefix"] == "%m [%p] # not a comment "
    assert conf["listen_addresses"] == "*"
    assert "jit_above_cost" not in conf


def test_fault_case_dataclass_round_trip():
    c = load_cases()[0]
    assert isinstance(c, FaultCase)
    d = c.to_dict()
    assert d["case_id"] == "c01_shared_buffers" and d["restart_required"] is True


# ---------------------------------------------------------------------------
# state file


def test_state_file_round_trip(tmp_path):
    path = tmp_path / "faults" / "state.json"
    assert inject.load_state(path) == inject.empty_state()
    state = inject.empty_state()
    state["case"] = {"case_id": "c02_work_mem", "status": "applied"}
    inject.save_state(path, state)
    loaded = inject.load_state(path)
    assert loaded["case"]["case_id"] == "c02_work_mem"
    assert loaded["updated_at"]
    assert not list(path.parent.glob("*.tmp"))
    inject.clear_state(path)
    assert not path.exists()
    inject.clear_state(path)  # idempotent


def test_state_file_lives_under_gitignored_data():
    cfg = get_config()
    path = inject.default_state_path(cfg)
    assert path == cfg.data_dir / "faults" / "state.json"
    assert "/data/" in (cfg.root / ".gitignore").read_text().splitlines()


def test_state_file_version_is_checked(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"version": 99}))
    with pytest.raises(inject.FaultError):
        inject.load_state(path)


# ---------------------------------------------------------------------------
# a fake server: baseline file + ALTER SYSTEM layer, reload and restart semantics


class FakeServer:
    def __init__(self, baseline: dict[str, str]) -> None:
        self.baseline = dict(baseline)
        self.auto: dict[str, str] = {}
        self.active = dict(baseline)
        self.log: list[str] = []
        self.refuse_on_reload: set[str] = set()
        self.reject_alter: set[str] = set()
        self.mem_limit = 256 * 2**20

    def _target(self) -> dict[str, str]:
        return {**self.baseline, **self.auto}

    def info(self, name):
        if name not in INFOS:
            raise KeyError(name)
        return INFOS[name]

    def running(self, name):
        info = self.info(name)
        v = units.normalize(self.active.get(name, "0"), info)
        return units.format_number(v) if isinstance(v, float) else v

    def auto_conf(self):
        return dict(self.auto)

    def alter_system(self, name, value):
        self.info(name)
        if name in self.reject_alter and value is not None:
            raise psycopg.errors.InvalidParameterValue(f"invalid value for {name}")
        self.log.append(f"alter {name}={value}")
        if value is None:
            self.auto.pop(name, None)
        else:
            self.auto[name] = value

    def reset_all(self):
        self.log.append("reset_all")
        self.auto.clear()

    def reload(self):
        self.log.append("reload")
        for name, value in self._target().items():
            if self.info(name).context != "postmaster" and name not in self.refuse_on_reload:
                self.active[name] = value

    def restart(self):
        self.log.append("restart")
        self.active = self._target()

    def analyze(self):
        self.log.append("analyze")

    def sleep(self, seconds):
        pass

    def baseline_problems(self):
        return []

    def memory_limit_bytes(self):
        return self.mem_limit

    def reset_statement_stats(self):
        self.log.append("reset_statements")


@pytest.fixture()
def fake():
    base = baseline_config_values()
    return FakeServer({**base, "checkpoint_timeout": "300", "wal_level": "replica", "jit": "on"})


@pytest.fixture()
def state_path(tmp_path):
    return tmp_path / "state.json"


def test_apply_and_reset_with_fake_server(fake, state_path):
    case = get_case("c02_work_mem")
    out = inject.apply_case(case, srv=fake, state_path=state_path)
    assert out.case_id == "c02_work_mem"
    assert fake.log[:3] == ["reset_all", "alter work_mem=64kB", "restart"]
    assert "reload" not in fake.log  # a reload would log 'parameter "work_mem" changed'
    assert fake.log[-2:] == ["analyze", "reset_statements"]
    assert fake.running("work_mem") == "64"
    state = inject.load_state(state_path)
    assert state["case"]["case_id"] == "c02_work_mem" and state["case"]["status"] == "applied"
    assert inject.verify(case, srv=fake, state_path=state_path) == []
    assert inject.verify(get_case("c03"), srv=fake, state_path=state_path)  # wrong case

    inject.reset(srv=fake, state_path=state_path)
    assert fake.auto == {}
    assert fake.running("work_mem") == "8192"
    assert not state_path.exists()
    assert inject.verify(None, srv=fake, state_path=state_path) == []


def test_apply_as_required_reloads(fake, state_path):
    out = inject.apply_case("c03", activation="as_required", srv=fake, state_path=state_path)
    assert out.details["activation"] == "reload"
    assert "restart" not in fake.log
    assert fake.running("random_page_cost") == "100"
    out = inject.apply_case("c01", activation="as_required", srv=fake, state_path=state_path)
    assert out.details["activation"] == "restart"
    assert fake.auto == {"shared_buffers": "1MB"}
    assert fake.running("random_page_cost") == "1.1"  # the previous case is gone
    with pytest.raises(ValueError):
        inject.apply_case("c01", activation="sometimes", srv=fake, state_path=state_path)


def test_every_case_applies_and_resets(fake, state_path):
    for case in load_cases():
        inject.apply_case(case, srv=fake, state_path=state_path)
        assert fake.auto == {case.setting: case.bad_value}
        assert inject.verify(case, srv=fake, state_path=state_path) == []
        others = [c for c in load_cases() if c is not case]
        assert all(inject.verify(o, srv=fake, state_path=state_path) for o in others)
    inject.reset(srv=fake, state_path=state_path)
    assert inject.verify(None, srv=fake, state_path=state_path) == []


def test_verify_detects_drift(fake, state_path):
    case = get_case("c04")
    inject.apply_case(case, srv=fake, state_path=state_path)
    fake.active["work_mem"] = "4MB"  # someone changed something else
    problems = inject.verify(case, srv=fake, state_path=state_path)
    assert any("work_mem" in p for p in problems)
    fake.active["work_mem"] = "8MB"
    fake.auto["max_parallel_workers_per_gather"] = "1"  # wrong value in auto.conf
    assert inject.verify(case, srv=fake, state_path=state_path)


def test_reset_fails_loudly_when_baseline_not_restored(fake, state_path):
    inject.apply_case(get_case("c02"), srv=fake, state_path=state_path)
    fake.baseline["work_mem"] = "16MB"  # the server's file no longer matches the repo's
    with pytest.raises(inject.FaultError, match="work_mem"):
        inject.reset(srv=fake, state_path=state_path)
    assert state_path.exists()  # the state is kept until a reset succeeds


def test_set_and_undo_a_fix_of_the_broken_setting(fake, state_path):
    """Undoing a fix of the broken setting restores the case's bad value, not the baseline."""
    inject.apply_case(get_case("c02_work_mem"), srv=fake, state_path=state_path)
    change = inject.set_setting("work_mem", "8MB", srv=fake, state_path=state_path)
    assert change.previous == "64kB" and change.method == "reload"
    assert change.running == "8192" and change.before_running == "64"
    assert fake.running("work_mem") == "8192"
    undone = inject.undo(srv=fake, state_path=state_path)
    assert undone.name == "work_mem"
    assert fake.auto == {"work_mem": "64kB"}
    assert fake.running("work_mem") == "64"
    assert inject.verify(get_case("c02"), srv=fake, state_path=state_path) == []
    assert inject.undo(srv=fake, state_path=state_path) is None


def test_set_and_undo_a_different_setting(fake, state_path):
    inject.apply_case(get_case("c02_work_mem"), srv=fake, state_path=state_path)
    inject.set_setting("random_page_cost", "4", srv=fake, state_path=state_path)
    assert fake.auto == {"work_mem": "64kB", "random_page_cost": "4"}
    assert inject.verify(None, srv=fake, state_path=state_path) == []  # stacked change known
    inject.undo(srv=fake, state_path=state_path)
    assert fake.auto == {"work_mem": "64kB"}  # removed again, the case stays
    assert fake.running("random_page_cost") == "1.1"
    assert inject.verify(get_case("c02"), srv=fake, state_path=state_path) == []


def test_set_a_non_target_setting(fake, state_path):
    inject.apply_case(get_case("c05_jit_above_cost"), srv=fake, state_path=state_path)
    with inject.applied_setting("jit", "off", srv=fake, state_path=state_path) as c:
        assert c.method == "reload" and c.previous is None
        assert fake.running("jit") == "off"
    assert fake.running("jit") == "on"
    assert fake.auto == {"jit_above_cost": "0"}


def test_set_restart_setting_restarts_and_undo_restarts(fake, state_path):
    inject.apply_case(get_case("c01_shared_buffers"), srv=fake, state_path=state_path)
    fake.log.clear()
    with inject.applied_setting("shared_buffers", "64MB", srv=fake, state_path=state_path) as c:
        assert c.method == "restart"
        assert fake.running("shared_buffers") == "8192"
    assert fake.log.count("restart") == 2
    assert fake.running("shared_buffers") == "128"
    assert fake.auto == {"shared_buffers": "1MB"}
    assert inject.verify(get_case("c01"), srv=fake, state_path=state_path) == []


def test_restart_fix_on_top_of_a_reload_case(fake, state_path):
    inject.apply_case(get_case("c02"), srv=fake, state_path=state_path)
    fake.log.clear()
    change = inject.set_setting("shared_buffers", "128MB", srv=fake, state_path=state_path)
    assert change.method == "restart"
    assert fake.running("work_mem") == "64"  # the case survives the restart
    inject.undo(srv=fake, state_path=state_path)
    assert fake.auto == {"work_mem": "64kB"}
    assert fake.running("shared_buffers") == "8192"


def test_stacked_changes_undo_in_reverse_order(fake, state_path):
    inject.apply_case(get_case("c02"), srv=fake, state_path=state_path)
    inject.set_setting("work_mem", "1MB", srv=fake, state_path=state_path)
    inject.set_setting("work_mem", "16MB", srv=fake, state_path=state_path)
    inject.set_setting("random_page_cost", "2", srv=fake, state_path=state_path)
    undone = inject.undo_all(srv=fake, state_path=state_path)
    assert [c.name for c in undone] == ["random_page_cost", "work_mem", "work_mem"]
    assert fake.auto == {"work_mem": "64kB"}
    assert fake.running("work_mem") == "64"
    assert inject.load_state(state_path)["overrides"] == []


def test_set_analyzes_only_for_statistics_target(fake, state_path):
    inject.apply_case(get_case("c06"), srv=fake, state_path=state_path)
    fake.log.clear()
    inject.set_setting("default_statistics_target", "100", srv=fake, state_path=state_path)
    assert "analyze" in fake.log
    fake.log.clear()
    inject.undo(srv=fake, state_path=state_path)
    assert "analyze" in fake.log  # the bad statistics are back
    assert fake.running("default_statistics_target") == "1"
    fake.log.clear()
    inject.set_setting("work_mem", "64MB", srv=fake, state_path=state_path)
    assert "analyze" not in fake.log


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("no_such_setting", "1", "unknown setting"),
        ("work_mem; DROP TABLE x", "1", "not a valid setting name"),
        ("work_mem", "1kB", "outside the allowed range"),
        ("work_mem", "lots", "not a number"),
        ("work_mem", "32mb", "invalid unit"),
        ("work_mem", "", "empty value"),
        ("random_page_cost", "-2", "outside the allowed range"),
        ("default_statistics_target", "0", "outside the allowed range"),
        ("jit", "maybe", "not a boolean"),
        ("server_version_num", "1", "internal"),
        ("port", "5434", "harness"),
        ("shared_buffers", "1GB", "memory limit"),
    ],
)
def test_invalid_fixes_are_rejected_without_changing_anything(fake, state_path, name, value, match):
    inject.apply_case(get_case("c02"), srv=fake, state_path=state_path)
    before_auto, before_active, n_log = dict(fake.auto), dict(fake.active), len(fake.log)
    with pytest.raises(InvalidFix, match=match):
        inject.set_setting(name, value, srv=fake, state_path=state_path)
    attempt = inject.try_fix(name, value, srv=fake, state_path=state_path)
    assert not attempt.applied and attempt.error_kind == "invalid" and match in attempt.error
    assert fake.auto == before_auto and fake.active == before_active
    assert len(fake.log) == n_log  # no ALTER SYSTEM, reload or restart happened
    state = inject.load_state(state_path)
    assert state["overrides"] == []
    assert state["rejected"][-1]["name"] == name and state["case"]["case_id"] == "c02_work_mem"
    assert inject.verify(get_case("c02"), srv=fake, state_path=state_path) == []


def test_llm_style_spelling_is_cleaned(fake, state_path):
    inject.apply_case(get_case("c02"), srv=fake, state_path=state_path)
    attempt = inject.try_fix(" Work_Mem ", "'8MB';", srv=fake, state_path=state_path)
    assert attempt.applied, attempt.error
    assert attempt.change["name"] == "work_mem" and attempt.change["value"] == "8MB"
    assert fake.running("work_mem") == "8192"
    assert inject.clean_value(True) == "on"
    assert inject.clean_value('"1.1"') == "1.1"


def test_server_rejection_of_alter_system_is_an_invalid_fix(fake, state_path):
    inject.apply_case(get_case("c02"), srv=fake, state_path=state_path)
    fake.reject_alter.add("random_page_cost")
    attempt = inject.try_fix("random_page_cost", "2", srv=fake, state_path=state_path)
    assert not attempt.applied and attempt.error_kind == "invalid"
    assert "server rejected" in attempt.error
    assert fake.auto == {"work_mem": "64kB"}


def test_set_rolls_back_when_the_server_does_not_take_the_value(fake, state_path):
    inject.apply_case(get_case("c02"), srv=fake, state_path=state_path)
    fake.refuse_on_reload.add("random_page_cost")
    attempt = inject.try_fix("random_page_cost", "2", srv=fake, state_path=state_path)
    assert not attempt.applied and attempt.error_kind == "failed"
    assert "restored" in attempt.error
    assert fake.auto == {"work_mem": "64kB"}
    assert inject.load_state(state_path)["overrides"] == []
    assert inject.verify(get_case("c02"), srv=fake, state_path=state_path) == []


# ---------------------------------------------------------------------------
# measurement rows and table


def _step(total: float, comps: dict[str, float], settings=None, cv=2.0) -> dict:
    return {
        "total_median": total,
        "total_cv_pct": cv,
        "component_medians": comps,
        "settings": settings or {},
        "errors": [],
        "bench_path": "x.json",
        "started_at": "2026-11-15T00:00:00Z",
        "finished_at": "2026-11-15T00:01:00Z",
        "temp_files": 0,
    }


def _row(pre=7.0, broken=10.0, fixed=7.2, post=7.4):
    case = get_case("c02_work_mem")
    comps = {c: 1.0 for c in COMPONENT_IDS}
    steps = {
        "baseline": _step(pre, comps),
        "broken": _step(broken, {**comps, "customer_reports": 4.0}, {"work_mem": "64kB"}),
        "fix": _step(fixed, comps),
        "post_reset": _step(post, comps),
    }
    return measure.build_row(case, steps, reset_config_ok=True, undo_ok=True, fix={"applied": True})


def test_build_row_and_table():
    row = _row()
    assert row["slowdown_pct"] == pytest.approx(3 / 7 * 100)
    assert row["primary_ratio"] == pytest.approx(4.0)
    assert row["ref_total_s"] == pytest.approx(7.2)
    assert row["fix_vs_ref_pct"] == pytest.approx(0.0)
    assert row["fix_recovered"] is True
    assert row["post_reset_vs_baseline_pct"] == pytest.approx(0.4 / 7 * 100)
    assert row["reset_ok"] is True
    bad_post = _row(post=8.0)
    assert bad_post["reset_ok"] is False and bad_post["reset_config_ok"] is True
    assert _row(fixed=8.5)["fix_recovered"] is False
    run = {"run": "full-x", "preset": "full", "started_at": "2026", "rows": [row]}
    table = measure.format_table(run)
    assert "| c02_work_mem | work_mem | 64kB | 8MB | no | 7.00 [2.0%] | 10.00 [2.0%] | +43% |" in (
        table
    )
    assert "customer_reports 1.00→4.00s (x4.0)" in table
    assert "| 7.20 [2.0%] | +0.0% |" in table
    assert "yes (cfg ok, +5.7%)" in table


def test_records_and_resume_helpers(tmp_path):
    path = tmp_path / "measure.jsonl"
    measure.append_record(path, {"type": "run", "run": "a", "preset": "full", "started_at": "t"})
    measure.append_record(path, {"type": "finished", "run": "a"})
    measure.append_record(path, {"type": "run", "run": "b", "preset": "full", "started_at": "t"})
    measure.append_record(path, {"type": "run", "run": "c", "preset": "quick", "started_at": "t"})
    with path.open("a") as fh:
        fh.write('{"type": "step", "run": "b", "kind"')  # cut short by an interruption
    records = measure.read_records(path)
    assert len(records) == 4
    assert measure.latest_run(records, preset="full", finished=False) == "b"
    assert measure.latest_run(records, preset="full", finished=True) == "a"
    assert measure.latest_run(records) == "c"
    assert [r["type"] for r in measure.run_records(records, "a")] == ["run", "finished"]


# --- multi-fault cases -------------------------------------------------------------------


def test_combo_case_reuses_single_cases():
    from pgdiag.faults.cases import all_cases, load_combos

    (m01,) = load_combos()
    c03, c04 = get_case("c03"), get_case("c04")
    assert m01.case_id == "m01_random_page_cost_max_parallel" and m01.is_multi
    assert m01.faults == (
        (c03.setting, c03.bad_value, c03.good_value),
        (c04.setting, c04.bad_value, c04.good_value),
    )
    assert m01.settings == ("random_page_cost", "max_parallel_workers_per_gather")
    assert get_case("m01") is m01 and get_case(m01.case_id) is m01
    assert len(load_cases()) == 6 and len(all_cases()) == 7  # the six stay the six
    assert get_case("random_page_cost") is c03  # setting lookups find the single case
    assert not c03.is_multi and c03.settings == ("random_page_cost",)


def test_parse_combos_rejects_bad_entries():
    from pgdiag.faults.cases import CaseError, parse_combos

    cases = load_cases()
    good = {"case_id": "m01_x", "combine": ["c03", "c04"], "rationale": "r"}
    assert parse_combos({"combos": [good]}, cases)[0].settings == (
        "random_page_cost",
        "max_parallel_workers_per_gather",
    )
    for bad in (
        {**good, "case_id": "m02_x"},  # numbering
        {**good, "combine": ["c03"]},  # one case is not a combo
        {**good, "combine": ["c03", "c03"]},  # same setting twice
        {**good, "rationale": ""},
        {**good, "primary_component": "pgbench"},  # not one of its components
    ):
        with pytest.raises(CaseError):
            parse_combos({"combos": [bad]}, cases)


def test_combo_applies_verifies_and_resets(fake, state_path):
    m01 = get_case("m01")
    inject.apply_case(m01, srv=fake, state_path=state_path)
    assert fake.auto == {"random_page_cost": "100", "max_parallel_workers_per_gather": "0"}
    assert inject.verify(m01, srv=fake, state_path=state_path) == []
    assert inject.load_state(state_path)["case"]["also"] == [
        ["max_parallel_workers_per_gather", "0", "2"]
    ]
    # fixing one of the two settings and undoing it puts that bad value back
    inject.set_setting("random_page_cost", "1.1", srv=fake, state_path=state_path)
    assert fake.auto["random_page_cost"] == "1.1"
    inject.undo(srv=fake, state_path=state_path)
    assert inject.verify(m01, srv=fake, state_path=state_path) == []
    # a single case is not the combo, and vice versa
    assert inject.verify(get_case("c03"), srv=fake, state_path=state_path)
    fake.active["max_parallel_workers_per_gather"] = "2"
    assert inject.verify(m01, srv=fake, state_path=state_path)
    inject.reset(srv=fake, state_path=state_path)
    assert fake.auto == {}
