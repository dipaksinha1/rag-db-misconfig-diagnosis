from pgdiag.settings import TARGET_SETTINGS


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
