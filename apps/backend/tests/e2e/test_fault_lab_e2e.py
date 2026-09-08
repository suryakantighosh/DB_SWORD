"""End-to-end fault-lab integration test (Arc C / A6).

For each fault scenario in FAULT_MATRIX, this test:
    1. Applies the fault to the fault-lab-db (a real Postgres 16 container
       preloaded with pg_stat_statements and the fault-lab schema).
    2. Drives an archetype workload against the injected fault so
       pg_stat_statements accumulates a signal (P0.5).
    3. Runs the diagnosis pipeline against that database using the same
       evidence-gathering path the production /diagnose endpoint uses.
    4. Asserts that primary_root_cause matches at least one of the labelled
       fault types for that scenario.

Success criterion: passes on >= MIN_PASS_THRESHOLD of SHIPPING_GATE_SCENARIOS.
Below that threshold the supervisor tiebreak or a specialist branch has
regressed and the diagnosis pipeline should not ship.

The test is skipped automatically when fault-lab-db is not reachable, so
`pytest` in a bare-workstation environment stays green.

Run with docker-compose up:
    docker-compose exec backend python -m pytest apps/backend/tests/e2e/test_fault_lab_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

import asyncpg
import pytest

from app.ml.rca_classifier.fault_lab.injector import (
    FAULT_MATRIX,
    FaultScenario,
    apply_fault,
)

# Canonical scenarios exercised in the ship gate. Skips faults whose real
# reproduction needs a held transaction (LOCK_CONTENTION) or an IO stress
# rig (IO_SATURATION, BUFFER_PRESSURE) that CI cannot supply cheaply.
SHIPPING_GATE_SCENARIOS = [
    "STALE_STATISTICS",
    "PLAN_FLIP",
    "CARDINALITY_MISESTIMATION",
    "VACUUM_LAG",
    "INDEX_MISSING",
    "INDEX_UNUSED",
]

MIN_PASS_THRESHOLD = 4   # >= 4 of 6 must land the correct primary_root_cause

# P0.5: per-scenario archetype workload. Each entry is a list of
# (sql, params) tuples driven against fault-lab-db AFTER apply_fault, so
# pg_stat_statements accumulates a top-slow-query signal the diagnosis
# pipeline can classify. Without this the pipeline sees an empty
# pg_stat_statements and every scenario xfails with NO_ACTIVE_INCIDENT.
#
# Query design maps 1:1 to the mutations apply_fault() performs on
# fault_lab_orders (see injector.py).
WORKLOAD_ARCHETYPES: dict[str, list[tuple[str, tuple[Any, ...]]]] = {
    # apply_fault: INSERTs 1000 rows at customer_id=999999 + SET STATISTICS 1.
    # Planner's histogram is now blind; querying the injected value
    # mispredicts cardinality.
    "STALE_STATISTICS": [
        ("SELECT id, amount FROM fault_lab_orders WHERE customer_id = $1", (999999,)),
    ],
    # apply_fault: INSERTs 5000 rows in customer_id [1000001..1005000].
    # Alternating parameters straddle the histogram boundary so the planner
    # flips between index scan and seq scan.
    "PLAN_FLIP": [
        ("SELECT id, amount FROM fault_lab_orders WHERE customer_id = $1", (5,)),
        ("SELECT id, amount FROM fault_lab_orders WHERE customer_id = $1", (1000001,)),
    ],
    # apply_fault: INSERTs 10000 rows at customer_id=7. Prior stats predict
    # ~100 rows; the self-join amplifies the underestimate downstream.
    "CARDINALITY_MISESTIMATION": [
        (
            "SELECT a.id FROM fault_lab_orders a "
            "JOIN fault_lab_orders b ON a.customer_id = b.customer_id "
            "WHERE a.customer_id = 7 LIMIT 100",
            (),
        ),
    ],
    # apply_fault: UPDATEs half the rows (id % 2 = 0). A range scan on
    # `amount` traverses the dead-tuple bloat left behind.
    "VACUUM_LAG": [
        ("SELECT COUNT(*) FROM fault_lab_orders WHERE amount > $1", (50,)),
    ],
    # apply_fault: DROPs fault_lab_orders_customer_id_idx. Equality lookup
    # falls back to a seq scan over 100k rows.
    "INDEX_MISSING": [
        ("SELECT COUNT(*) FROM fault_lab_orders WHERE customer_id = $1", (42,)),
    ],
    # apply_fault: (re)CREATEs the customer_id index. A low-selectivity
    # range predicate returning ~half the table makes the planner skip the
    # index and seq-scan anyway.
    "INDEX_UNUSED": [
        ("SELECT id FROM fault_lab_orders WHERE customer_id > $1 LIMIT 5000", (0,)),
    ],
}

# Iteration count: well above canary_monitor's `calls >= 5` filter, and
# enough total_exec_time that the archetype dominates the top-200 sort in
# pg_introspection.get_query_metrics.
WORKLOAD_ITERATIONS = 50


def _fault_lab_dsn() -> str:
    """DSN of the bundled fault-lab-db compose service."""
    return os.getenv(
        "FAULT_LAB_DSN",
        "postgresql://fault_lab:fault_lab_dev_password@fault-lab-db:5432/fault_lab",
    )


async def _fault_lab_reachable() -> bool:
    try:
        conn = await asyncpg.connect(_fault_lab_dsn(), timeout=3.0)
        await conn.fetchval("SELECT 1")
        await conn.close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def fault_lab_available() -> bool:
    if not asyncio.new_event_loop().run_until_complete(_fault_lab_reachable()):
        pytest.skip("fault-lab-db unreachable; skipping e2e integration test")
    return True


async def _drive_archetype_workload(
    conn: asyncpg.Connection, scenario_name: str
) -> int:
    """Run WORKLOAD_ITERATIONS of the archetype queries; return call count.

    Returns 0 when no archetype is defined for the scenario, so the caller
    can decide whether to skip.
    """
    archetypes = WORKLOAD_ARCHETYPES.get(scenario_name)
    if not archetypes:
        return 0
    calls = 0
    for _ in range(WORKLOAD_ITERATIONS):
        for sql, params in archetypes:
            await conn.execute(sql, *params)
            calls += 1
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario_name", SHIPPING_GATE_SCENARIOS)
async def test_diagnosis_matches_expected_root_cause(scenario_name: str, fault_lab_available: bool) -> None:
    """Apply a fault, drive the workload, run diagnosis, assert root cause."""
    from app.agents.graph_diagnosis import run_diagnosis  # local import — avoids app boot on collection
    from app.tools import pg_introspection

    scenario: FaultScenario = FAULT_MATRIX[scenario_name]
    dsn = _fault_lab_dsn()

    conn = await asyncpg.connect(dsn, timeout=10.0)
    try:
        await conn.execute("SELECT pg_stat_statements_reset()")
        await apply_fault(conn, scenario)

        # P0.5: drive the archetype workload so pg_stat_statements has a
        # signal for the diagnosis pipeline to classify. Without this step
        # every scenario xfails with NO_ACTIVE_INCIDENT.
        calls = await _drive_archetype_workload(conn, scenario_name)
        if calls == 0:
            pytest.skip(f"{scenario_name}: no archetype workload defined")

        # Update planner stats for scenarios whose fault mutates data volume
        # (STALE_STATISTICS deliberately keeps stats broken; leave it alone).
        if scenario_name != "STALE_STATISTICS":
            await conn.execute("ANALYZE fault_lab_orders")

        # Give the workload a moment to land in pg_stat_statements.
        await asyncio.sleep(1.0)

        query_metrics = await pg_introspection.get_query_metrics(conn, limit=200)
        table_metrics = await pg_introspection.get_table_statistics(conn)
    finally:
        await conn.close()

    evidence: dict[str, Any] = {
        "source": "live_postgresql",
        "connection_id": str(uuid.uuid4()),
        "query_metrics": [{**row, "query_text": row.get("query", "")} for row in query_metrics],
        "table_metrics": table_metrics,
        "plan_metrics": [],
        "timeline": [],
    }
    # graph_diagnosis.run_diagnosis is SYNC (LangGraph .invoke). Do not await.
    report = run_diagnosis(evidence)
    primary = report.get("primary_root_cause", "UNKNOWN")

    # Store the outcome on the item so the gate summary can total pass/fail.
    if not hasattr(test_diagnosis_matches_expected_root_cause, "results"):
        test_diagnosis_matches_expected_root_cause.results = {}  # type: ignore[attr-defined]
    test_diagnosis_matches_expected_root_cause.results[scenario_name] = primary  # type: ignore[attr-defined]

    # Accept the exact fault label or one of the labelled synonyms.
    expected = set(scenario.labels)
    if primary not in expected:
        # Soft-fail single scenarios; the gate check at the end enforces >= threshold.
        pytest.xfail(f"{scenario_name}: expected one of {expected}, got {primary}")


def test_shipping_gate_pass_rate() -> None:
    """Aggregate check: >= MIN_PASS_THRESHOLD scenarios must have matched."""
    results = getattr(test_diagnosis_matches_expected_root_cause, "results", {})
    if not results:
        pytest.skip("no scenario results recorded (upstream tests were skipped)")
    matched = [
        name for name, primary in results.items()
        if primary in set(FAULT_MATRIX[name].labels)
    ]
    detail = {name: results.get(name, "?") for name in SHIPPING_GATE_SCENARIOS}
    assert len(matched) >= MIN_PASS_THRESHOLD, (
        f"Diagnosis pipeline shipping gate failed: "
        f"{len(matched)}/{len(SHIPPING_GATE_SCENARIOS)} scenarios matched "
        f"(need >= {MIN_PASS_THRESHOLD}). Detail: {detail}"
    )
