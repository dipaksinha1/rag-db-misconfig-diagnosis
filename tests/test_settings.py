from pgdiag.settings import CASE_SETTINGS, NON_CASE_SETTINGS, TARGET_SETTINGS


def test_target_settings_are_exactly_the_ten_agreed_names():
    assert sorted(TARGET_SETTINGS) == sorted(
        [
            "shared_buffers",
            "work_mem",
            "effective_cache_size",
            "random_page_cost",
            "max_parallel_workers_per_gather",
            "autovacuum",
            "max_wal_size",
            "default_statistics_target",
            "jit_above_cost",
            "maintenance_work_mem",
        ]
    )
    assert len(TARGET_SETTINGS) == len(set(TARGET_SETTINGS)) == 10


def test_case_settings_are_the_six_experiment_settings_and_a_subset_of_the_targets():
    assert CASE_SETTINGS == (
        "shared_buffers",
        "work_mem",
        "random_page_cost",
        "max_parallel_workers_per_gather",
        "jit_above_cost",
        "default_statistics_target",
    )
    assert len(set(CASE_SETTINGS)) == 6
    assert set(CASE_SETTINGS) < set(TARGET_SETTINGS)
    assert set(NON_CASE_SETTINGS) == {
        "effective_cache_size",
        "autovacuum",
        "max_wal_size",
        "maintenance_work_mem",
    }
    assert set(NON_CASE_SETTINGS) | set(CASE_SETTINGS) == set(TARGET_SETTINGS)
