# apps/backend/tests/e2e/test_full_loop.py
FAULT_SCENARIOS = [
    "MISSING_COMPOSITE_INDEX",
    "STALE_STATISTICS",
    "VACUUM_BLOAT",
    "SEQ_SCAN_ON_LARGE_TABLE",
    "PLAN_FLIP_AFTER_UPDATE",
    "LOCK_CONTENTION",
    "BUFFER_CACHE_EVICTION",
    "AUTOVACUUM_STARVATION",
]

for scenario in FAULT_SCENARIOS:
    apply_fault(fault_lab_db, scenario)          # from injector.py
    inject_workload(fault_lab_db, seconds=120)   # generate telemetry
    diagnosis = trigger_diagnosis(connection_id)
    assert diagnosis.primary_root_cause == EXPECTED[scenario]  # gate 1
    recommendation = get_recommendation(diagnosis.id)
    experiment = trigger_simulation(recommendation.id)
    wait_for_verdict(experiment.id, timeout=90)
    assert experiment.policy_verdict in {"VERIFIED", "REJECTED"}  # gate 2, not INSUFFICIENT_DATA
    if experiment.policy_verdict == "VERIFIED":
        approve(experiment.id)
        deploy(experiment.id)
        canary = wait_for_canary_finish(experiment.id, timeout=300)
        assert canary.outcome in {"COMMIT", "ROLLBACK"}  # gate 3
        assert model_prediction_for(experiment.id).actual is not None  # gate 4, CHAIN 3
    revert_fault(fault_lab_db, scenario)