"""Deterministic recommendation generation from persisted diagnoses."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.connection import DatabaseConnection
from app.models.diagnosis import Diagnosis
from app.models.experiment import OptimizationExperiment
from app.models.telemetry import QueryMetric
from app.schemas.diagnosis import RecommendationOut


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_FROM_TABLE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_$]*)", re.IGNORECASE)
_WHERE_COLUMN = re.compile(
    r"\bwhere\s+(?:[A-Za-z_][A-Za-z0-9_$]*\.)?([A-Za-z_][A-Za-z0-9_$]*)\s*(?:=|>|<|\blike\b|\bin\b)\s*",
    re.IGNORECASE,
)
_WHERE_COLUMNS = re.compile(
    r"WHERE\s+(.+?)(?:\s+ORDER\s+BY|\s+GROUP\s+BY|\s+LIMIT|\s+HAVING|\s+RETURNING|\s*;|\s*$)",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_IN_PREDICATE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:=|<>|>=|<=|>|<|IN\s*\(|BETWEEN\b|LIKE\b|ILIKE\b|IS\b)",
    re.IGNORECASE,
)
_SQL_KEYWORDS = {
    "and", "or", "not", "null", "true", "false", "in", "between",
    "select", "from", "where", "group", "order", "by", "limit", "having",
    "as", "on", "join", "inner", "left", "right", "outer", "full",
    "when", "case", "then", "else", "end", "distinct", "count", "sum",
    "avg", "min", "max",
}


def _quoted_identifier(value: str | None) -> str | None:
    if not value or not _IDENTIFIER.fullmatch(value):
        return None
    # Identifiers are validated before interpolation; keeping them unquoted
    # also matches the strict candidate grammar used by the shadow executor.
    return value


def _candidate_from_query(query_text: str | None) -> tuple[str, str] | None:
    """Extract table + predicate columns and emit a composite CREATE INDEX candidate.

    Accepts pg_stat_statements-normalized queries (with $1/$2 placeholders) and
    infers up to three predicate columns for a composite index recommendation.
    """
    if not query_text:
        return None
    stripped = query_text.strip().rstrip(";")
    if ";" in stripped:
        return None  # multi-statement input — refuse
    table_match = _FROM_TABLE.search(stripped)
    table = _quoted_identifier(table_match.group(1) if table_match else None)
    if not table:
        return None
    columns: list[str] = []
    where_match = _WHERE_COLUMNS.search(stripped)
    if where_match:
        for raw_col in _COLUMN_IN_PREDICATE.findall(where_match.group(1)):
            if raw_col.lower() in _SQL_KEYWORDS:
                continue
            quoted = _quoted_identifier(raw_col)
            if quoted and quoted not in columns:
                columns.append(quoted)
    if not columns:
        column_match = _WHERE_COLUMN.search(stripped)
        col = _quoted_identifier(column_match.group(1) if column_match else None)
        if not col:
            return None
        columns = [col]
    col_expr = ", ".join(columns[:3])
    index_name = "zentrix_idx_" + hashlib.sha256(f"{table}:{col_expr}".encode()).hexdigest()[:12]
    # CONCURRENTLY is the safer choice for large hot production tables (no
    # exclusive lock during build), but its "waiting for old snapshots" phase
    # never completes when Zentrix self-monitors — every backend/worker
    # connection to the same DB keeps CIC blocked forever.
    #
    # Default: plain CREATE INDEX (sub-second on a 100k-row table, brief AEL,
    # never hangs). Set ZENTRIX_INDEX_CONCURRENTLY=true in production where
    # Zentrix's metadata DB is separate from the customer DB so CIC can drain.
    import os as _os
    _concurrently = _os.getenv("ZENTRIX_INDEX_CONCURRENTLY", "false").lower() == "true"
    _cc = "CONCURRENTLY " if _concurrently else ""
    # IF NOT EXISTS makes retries idempotent — the recommendation's index
    # name is a deterministic hash of (table, cols), so a second click after
    # a successful first deploy would otherwise fail with DuplicateTableError.
    # The canary observation window still runs; it just observes an already-
    # applied index instead of applying and then observing.
    return table, f"CREATE INDEX {_cc}IF NOT EXISTS {index_name} ON {table} ({col_expr});"


# Tables that belong to Zentrix itself. When self-monitoring (demo mode),
# pg_stat_statements sees INSERTs/SELECTs against these and would otherwise
# feed them into recommendations. We never want to index our own telemetry
# tables from a customer-facing recommendation.
_ZENTRIX_INTERNAL_TABLES = (
    "query_metrics", "table_metrics", "plan_metrics",
    "canary_runs", "optimization_experiments", "diagnoses",
    "recommendations", "audit_logs", "audit_log",
    "model_predictions", "model_drift_reports",
    "bandit_events", "forecast_records", "roi_records",
    "approvals", "database_connections", "users",
    "alembic_version",
)


def _mentions_internal_table(sql: str) -> bool:
    """Return True if the SQL text references any Zentrix-owned table."""
    lowered = sql.lower()
    return any(f" {name} " in lowered or f" {name}(" in lowered
               or f"\"{name}\"" in lowered or f".{name} " in lowered
               for name in _ZENTRIX_INTERNAL_TABLES)


async def _top_query(connection_id: uuid.UUID, db: AsyncSession) -> QueryMetric | None:
    # 1st source: persisted query_metrics (populated on TELEMETRY_POLL_INTERVAL_SECONDS cadence,
    # default 60s). A freshly-run diagnosis can outrun this table; hence the live fallback below.
    statement = (
        select(QueryMetric)
        .where(
            QueryMetric.connection_id == connection_id,
            QueryMetric.query_text.is_not(None),
            or_(QueryMetric.query_text.ilike("select%"), QueryMetric.query_text.ilike("with%")),
        )
        .order_by(QueryMetric.total_exec_time.desc())
        .limit(20)
    )
    rows = (await db.scalars(statement)).all()
    for row in rows:
        if not _mentions_internal_table(row.query_text or ""):
            return row

    # 2nd source (fallback): live pg_stat_statements via the customer asyncpg pool.
    # This closes the "fresh diagnosis, empty recommendation" gap when telemetry-collector
    # has not yet persisted the top query into query_metrics.
    try:
        from app.db.customer_db import customer_connection_manager
        from app.tools.pg_introspection import get_query_metrics as _live_get_query_metrics
        pool = await customer_connection_manager.get_customer_pool(connection_id, db)
        async with pool.acquire() as customer:
            live_rows = await _live_get_query_metrics(customer, limit=20)
        for r in live_rows:
            text = (r.get("query") or "").strip()
            if not text:
                continue
            lowered = text.lower()
            if not (lowered.startswith("select") or lowered.startswith("with")):
                continue
            if _mentions_internal_table(text):
                continue
            # Build an ad-hoc QueryMetric-shaped object with the fields _candidate_from_query needs.
            live_metric = type(
                "LiveQueryMetric",
                (),
                {"query_text": text, "connection_id": connection_id},
            )()
            return live_metric  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001 — fallback is best-effort
        try:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "Live pg_stat_statements fallback failed for connection %s: %s", connection_id, exc
            )
        except Exception:
            pass
    return None


async def recommendations_for_diagnosis(
    diagnosis: Diagnosis,
    db: AsyncSession,
) -> list[RecommendationOut]:
    """Build only actionable candidates supported by the diagnosis evidence."""
    cause = diagnosis.primary_root_cause.upper()
    plan: dict[str, Any] = diagnosis.validation_plan or {}
    affected = plan.get("affected_object")
    table = _quoted_identifier(str(affected).split(".")[-1] if affected not in {None, "database"} else None)

    candidate_type: str | None = None
    title: str | None = None
    candidate_sql: str | None = None
    predicted_impact: str | None = None
    risk = "Low"

    if cause in {"STALE_STATISTICS", "CARDINALITY_MISESTIMATION", "PLAN_FLIP"}:
        candidate_type = "STATISTICS"
        title = f"Refresh planner statistics{f' for {table}' if table else ''}"
        candidate_sql = f"ANALYZE {table};" if table else "ANALYZE;"
        predicted_impact = "May improve cardinality estimates and stabilize query plans."
    elif cause in {"VACUUM_LAG", "BLOAT"}:
        candidate_type = "VACUUM"
        title = f"Vacuum and analyze{f' {table}' if table else ' the affected relation'}"
        candidate_sql = f"VACUUM ANALYZE {table};" if table else None
        predicted_impact = "May reduce dead-tuple pressure and refresh planner statistics."
        risk = "Medium"
    elif cause == "INDEX_MISSING":
        query = await _top_query(diagnosis.connection_id, db)
        candidate = _candidate_from_query(query.query_text if query else None)
        if candidate:
            table, candidate_sql = candidate
            candidate_type = "INDEX"
            title = f"Add a selective index on {table}"
            predicted_impact = "May reduce sequential scans for the observed filtered workload."
            risk = "Medium"

    if not candidate_type or not title or not candidate_sql or not predicted_impact:
        return []

    recommendation_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"zentrix:recommendation:{diagnosis.id}:{candidate_sql}",
    )
    experiment = await db.scalar(
        select(OptimizationExperiment)
        .where(
            OptimizationExperiment.diagnosis_id == diagnosis.id,
            OptimizationExperiment.candidate_sql == candidate_sql,
        )
        .order_by(OptimizationExperiment.created_at.desc())
        .limit(1)
    )
    uncertainty = round(max(0.0, min(100.0, (1.0 - diagnosis.confidence) * 100.0)), 1)
    return [
        RecommendationOut(
            id=recommendation_id,
            diagnosis_id=diagnosis.id,
            connection_id=diagnosis.connection_id,
            diagnosis_title=diagnosis.title,
            primary_root_cause=cause,
            type=candidate_type,
            title=title,
            rationale=(
                f"Generated from the persisted {cause} diagnosis and its live PostgreSQL evidence. "
                "The candidate must pass shadow replay and policy verification before approval."
            ),
            predicted_impact=predicted_impact,
            uncertainty_pct=uncertainty,
            risk=risk,
            candidate_sql=candidate_sql,
            experiment_id=experiment.id if experiment else None,
        )
    ]


async def recommendations_for_connection(
    connection_id: uuid.UUID | None,
    db: AsyncSession,
) -> list[RecommendationOut]:
    statement = select(Diagnosis).order_by(Diagnosis.created_at.desc())
    if connection_id:
        statement = statement.where(Diagnosis.connection_id == connection_id)
    diagnoses = (await db.scalars(statement)).all()
    recommendations: list[RecommendationOut] = []
    for diagnosis in diagnoses:
        recommendations.extend(await recommendations_for_diagnosis(diagnosis, db))
    return recommendations


async def recommendations_for_user(
    user_id: uuid.UUID,
    db: AsyncSession,
) -> list[RecommendationOut]:
    statement = (
        select(Diagnosis)
        .join(DatabaseConnection, DatabaseConnection.id == Diagnosis.connection_id)
        .where(DatabaseConnection.user_id == user_id)
        .order_by(Diagnosis.created_at.desc())
    )
    diagnoses = (await db.scalars(statement)).all()
    recommendations: list[RecommendationOut] = []
    for diagnosis in diagnoses:
        recommendations.extend(await recommendations_for_diagnosis(diagnosis, db))
    return recommendations
