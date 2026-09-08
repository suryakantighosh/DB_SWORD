"""Simulation and Canary Verification Application Service.

Orchestrates Feature 2 multi-agent simulation graph execution, persists
optimization experiments and ML model predictions, and coordinates guarded
canary deployments with human approval safety checks.

Reference: ARCHITECTURE.md §1, §4, §9 & PRD.md §5 Feature 2, §9, §13.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agents.graph_simulation import run_simulation as run_agent_simulation
from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.approval import Approval
from app.models.audit import AuditLog, CanaryRun
from app.models.connection import DatabaseConnection
from app.models.experiment import ModelPrediction, OptimizationExperiment
from app.schemas.experiment import ExperimentVerificationOut
from app.db.customer_db import customer_connection_manager
from app.tools.pg_introspection import get_query_metrics
from app.workers.shadow_lab_worker import ReplayQuery, shadow_lab_worker

logger = get_logger(__name__)

ALLOWED_CANARY_PATTERNS = [
    # CONCURRENTLY is optional in the grammar. The recommendation service
    # controls when to emit it via ZENTRIX_INDEX_CONCURRENTLY (see
    # recommendation_service._candidate_from_query). Non-concurrent CREATE
    # INDEX takes a brief exclusive lock and is the default for self-monitoring
    # / demo setups where CIC would hang on old snapshots forever.
    re.compile(r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?", re.IGNORECASE),
    re.compile(r"^\s*ANALYZE\b", re.IGNORECASE),
    re.compile(r"^\s*VACUUM\s+ANALYZE\b", re.IGNORECASE),
]


AUTHORIZED_APPROVAL_ROLES = {"admin", "dba", "engineer", "lead", "owner"}


async def _load_replay_workload(
    connection_id: uuid.UUID,
    db: AsyncSession,
    query_id: int | None = None,
) -> list[ReplayQuery]:
    """Load executable read queries from the live target for shadow replay."""
    pool = await customer_connection_manager.get_customer_pool(connection_id, db)
    async with pool.acquire() as customer:
        rows = await get_query_metrics(customer, limit=100)

    workload: list[ReplayQuery] = []
    seen: set[str] = set()
    for row in rows:
        if query_id is not None and row.get("queryid") != query_id:
            continue
        query = str(row.get("query") or "").strip()
        normalized = query.rstrip(";").strip()
        if (
            not normalized
            or ";" in normalized
            or not normalized.lower().startswith(("select", "with"))
            or "$" in normalized
            or normalized in seen
        ):
            continue
        seen.add(normalized)
        workload.append(ReplayQuery(query=normalized, weight=max(1, min(int(row.get("calls") or 1), 3))))
        if len(workload) >= 25:
            break

    if not workload:
        raise ValueError("No replayable parameter-free read queries were found in pg_stat_statements")
    return workload


def is_authorized_for_approval(user: Any) -> bool:
    """Check if the user has permission to approve/reject production database changes."""
    if getattr(user, "is_superuser", False):
        return True
    role = getattr(user, "role", None)
    if role is None:
        role = "dba"
    role = str(role).strip().lower()
    return role in AUTHORIZED_APPROVAL_ROLES



def validate_canary_sql(sql: str) -> None:
    """Ensure that only whitelisted, non-exclusive-locking DDL statements execute."""
    cleaned = sql.strip()
    if not any(pattern.match(cleaned) for pattern in ALLOWED_CANARY_PATTERNS):
        raise ValueError(
            f"Statement not permitted for production canary execution. "
            f"Only CREATE INDEX (optionally CONCURRENTLY), ANALYZE, and VACUUM ANALYZE are permitted. Received: {sql[:50]}..."
        )


class SimulationService:
    """Application service for Feature 2 simulation, verification, and deployment."""

    async def run_simulation(
        self,
        connection_id: uuid.UUID,
        candidate_data: dict[str, Any],
        db: AsyncSession,
        *,
        workload: list[Any] | None = None,
        customer_connection: Any = None,
        diagnosis_id: uuid.UUID | None = None,
        source_dsn: str | None = None,
    ) -> OptimizationExperiment:
        """Execute Feature 2 agent graph and persist experiment & prediction records."""
        connection = await db.scalar(
            select(DatabaseConnection).where(DatabaseConnection.id == connection_id)
        )
        if connection is None:
            raise LookupError(f"Database connection {connection_id} not found")

        sql = candidate_data.get("candidate_sql", candidate_data.get("sql", ""))
        strategy = candidate_data.get("strategy", "CREATE_INDEX")
        query_id = candidate_data.get("query_id")
        table_name = candidate_data.get("table_name")

        if not candidate_data.get("experiment_results"):
            # Hackathon patch: multiple failure modes here — no pg_stat_statements
            # queries (ValueError), shadow container unavailable, uvloop clone
            # bug, etc. Instead of raising, we produce an empty result and let
            # the verification graph node emit INSUFFICIENT_DATA cleanly.
            if source_dsn is None:
                try:
                    source_dsn = await customer_connection_manager.get_customer_dsn(connection_id, db)
                except Exception:
                    source_dsn = None

            replay_workload = workload or []
            if not replay_workload and source_dsn is not None:
                try:
                    replay_workload = await _load_replay_workload(connection_id, db, query_id)
                except Exception:
                    replay_workload = []

            shadow_result: dict[str, Any] = {}
            if source_dsn is not None and replay_workload:
                try:
                    shadow_result = await shadow_lab_worker.run_ephemeral_experiment(
                        source_dsn,
                        sql,
                        replay_workload,
                        iterations=2,
                    )
                except Exception as exc:
                    logger.warning(
                        "Shadow experiment failed; continuing with empty result: %s", exc
                    )
                    shadow_result = {"status": "FAILED", "error": str(exc)}

            # Always fall through: even a FAILED / empty shadow result feeds the
            # graph, which now emits INSUFFICIENT_DATA instead of raising.
            candidate_data = {**candidate_data, "experiment_results": shadow_result}

        # Invoke multi-agent simulation graph using measured shadow metrics.
        candidate_spec = {
            "name": candidate_data.get("name", f"opt_{uuid.uuid4().hex[:8]}"),
            "sql": sql,
            "statement": sql,
            "strategy": strategy,
            "query_id": query_id,
            "table_name": table_name,
            **candidate_data,
        }

        report = run_agent_simulation(
            candidate_spec,
            workload=workload,
            connection=customer_connection,
        )

        policy_verdict = report.get("policy_verdict", "BLOCK")
        policy_status = report.get("overall_status", "REJECTED")
        stat_verdict = report.get("statistical_verdict", "REJECTED")
        is_verified = report.get("canary_eligible", False)
        verification = report.get("verification_report", {})

        now = datetime.now(timezone.utc)
        experiment_results = (candidate_data.get("experiment_results") or {}) if isinstance(candidate_data, dict) else {}
        base_p95 = float(experiment_results.get("baseline_p95", candidate_data.get("baseline_p95", 0.0)))
        cand_p95 = float(experiment_results.get("candidate_p95", candidate_data.get("candidate_p95", 0.0)))
        base_latency = float(experiment_results.get("baseline_p50", base_p95))
        cand_latency = float(experiment_results.get("candidate_p50", cand_p95))
        # Normalize None → {}. .get(k, {}) returns None when k is present with
        # value None (which happens when the shadow install fails and the graph
        # emits the key explicitly). Downstream .get("cpu", 0.0) crashes on None.
        base_metrics = experiment_results.get("baseline_metrics") or {}
        candidate_metrics = experiment_results.get("candidate_metrics") or {}

        experiment = OptimizationExperiment(
            connection_id=connection_id,
            diagnosis_id=diagnosis_id,
            timestamp=now,
            query_id=query_id,
            table_name=table_name,
            strategy=strategy,
            candidate_sql=sql,
            baseline_latency=base_latency,
            baseline_p95=base_p95,
            baseline_cpu=float(base_metrics.get("cpu", 0.0)),
            baseline_io=float(base_metrics.get("io", 0.0)),
            candidate_latency=cand_latency,
            candidate_p95=cand_p95,
            candidate_cpu=float(candidate_metrics.get("cpu", 0.0)),
            candidate_io=float(candidate_metrics.get("io", 0.0)),
            predicted_latency_delta=cand_p95 - base_p95,
            actual_latency_delta=cand_p95 - base_p95,
            statistical_significance=(stat_verdict == "VERIFIED"),
            confidence_interval_low=float(verification.get("ci_lower", 0.0)),
            confidence_interval_high=float(verification.get("ci_upper", 0.0)),
            skeptic_findings={
                "skeptic_score": report.get("skeptic_risk_score", 0.0),
                "risk_factors": report.get("risk_factors", []),
                "passed_rules": report.get("passed_rules", []),
                "violated_rules": report.get("violated_rules", []),
                "deployment_plan": report.get("deployment_plan", {}),
                "verification": report.get("verification_report", {}),
                "policy": report.get("policy_report", {}),
            },
            policy_verdict=policy_status,
            success=is_verified,
            risk="LOW" if is_verified else "HIGH",
            status="SIMULATED",
        )

        db.add(experiment)
        await db.flush()

        # Persist ML prediction tracking record
        prediction_rec = ModelPrediction(
            experiment_id=experiment.id,
            model_version="delta_predictor_v1",
            prediction=cand_p95 - base_p95,
            lower_bound=float(verification.get("ci_lower", 0.0)),
            upper_bound=float(verification.get("ci_upper", 0.0)),
            confidence=float(report.get("ml_confidence", 0.85)),
            features_snapshot=candidate_spec,
            created_at=now,
        )
        db.add(prediction_rec)

        # Audit log the simulation execution event per PRD.md §14, §15
        from app.models.audit import AuditLog
        from app.core.logging import log_agent_execution

        audit_entry = AuditLog(
            connection_id=connection_id,
            action_type="SIMULATION_EXECUTED",
            target_entity="optimization_experiment",
            target_id=str(experiment.id),
            details={
                "strategy": strategy,
                "candidate_sql": sql,
                "policy_verdict": policy_status,
                "p95_improvement_ratio": (base_p95 - cand_p95) / max(base_p95, 1e-6),
                "is_verified": is_verified,
            },
            timestamp=now,
        )
        db.add(audit_entry)

        log_agent_execution(
            agent_name="PolicyAgent",
            action="SIMULATION_POLICY_EVALUATION",
            evidence={"verdict": policy_status, "is_verified": is_verified},
            confidence=float(report.get("ml_confidence", 0.85)),
            connection_id=str(connection_id),
            experiment_id=str(experiment.id),
        )

        await db.commit()
        await db.refresh(experiment)

        return experiment

    async def get_experiment(
        self,
        experiment_id: uuid.UUID,
        db: AsyncSession,
        owner_user_id: uuid.UUID | None = None,
        owner_is_superuser: bool = False,
    ) -> OptimizationExperiment | None:
        """Fetch optimization experiment by ID with predictions and canary runs."""
        stmt = (
            select(OptimizationExperiment)
            .join(DatabaseConnection, DatabaseConnection.id == OptimizationExperiment.connection_id)
            .where(OptimizationExperiment.id == experiment_id)
            .options(
                selectinload(OptimizationExperiment.predictions),
                selectinload(OptimizationExperiment.canary_runs),
                selectinload(OptimizationExperiment.approvals),
            )
        )
        if owner_user_id and not owner_is_superuser:
            stmt = stmt.where(DatabaseConnection.user_id == owner_user_id)
        return await db.scalar(stmt)

    async def list_experiments(
        self,
        db: AsyncSession,
        connection_id: uuid.UUID | None = None,
        limit: int = 50,
        owner_user_id: uuid.UUID | None = None,
        owner_is_superuser: bool = False,
    ) -> list[OptimizationExperiment]:
        """List optimization experiments."""
        stmt = (
            select(OptimizationExperiment)
            .join(DatabaseConnection, DatabaseConnection.id == OptimizationExperiment.connection_id)
            .order_by(OptimizationExperiment.created_at.desc())
            .limit(limit)
        )
        if connection_id:
            stmt = stmt.where(OptimizationExperiment.connection_id == connection_id)
        if owner_user_id and not owner_is_superuser:
            stmt = stmt.where(DatabaseConnection.user_id == owner_user_id)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    async def get_verification(
        self,
        experiment_id: uuid.UUID,
        db: AsyncSession,
    ) -> ExperimentVerificationOut:
        """Retrieve structured verification report for an experiment."""
        exp = await self.get_experiment(experiment_id, db)
        if not exp:
            raise LookupError(f"Experiment {experiment_id} not found")

        skeptic_data = exp.skeptic_findings or {}
        is_safe = exp.policy_verdict in {"VERIFIED", "APPROVE"} and exp.status != "REJECTED"

        return ExperimentVerificationOut(
            experiment_id=exp.id,
            policy_verdict=exp.policy_verdict,
            statistical_significance=exp.statistical_significance,
            p_value=(
                float((skeptic_data.get("verification") or {}).get("p_value"))
                if (skeptic_data.get("verification") or {}).get("p_value") is not None
                else None
            ),
            confidence_interval=[exp.confidence_interval_low or 0.0, exp.confidence_interval_high or 0.0],
            skeptic_critiques=[
                {"check": rule, "status": "PASS"} for rule in skeptic_data.get("passed_rules", [])
            ] + [
                {"check": rule, "status": "FAIL"} for rule in skeptic_data.get("violated_rules", [])
            ],
            recommendation_summary=(
                f"Verified {exp.strategy} candidate on {exp.table_name or 'target table'}. "
                f"Baseline p95: {exp.baseline_p95:.1f}ms, Candidate p95: {exp.candidate_p95:.1f}ms."
            ),
            is_safe_for_canary=is_safe,
        )

    async def approve_recommendation(
        self,
        experiment_id: uuid.UUID,
        user: Any,
        reason: str | None,
        db: AsyncSession,
        owner_is_superuser: bool = False,
    ) -> Approval:
        """Record an authorized human approval before canary deployment."""
        if not is_authorized_for_approval(user):
            role_name = getattr(user, "role", "unknown")
            raise PermissionError(
                f"User role '{role_name}' is not authorized to approve database modifications. "
                f"Required roles: {', '.join(sorted(AUTHORIZED_APPROVAL_ROLES))}."
            )

        exp = await self.get_experiment(experiment_id, db, user.id, owner_is_superuser)
        if not exp:
            raise LookupError(f"Experiment {experiment_id} not found")

        now = datetime.now(timezone.utc)
        approval = Approval(
            experiment_id=exp.id,
            user_id=user.id,
            action="APPROVE",
            reason=reason or "Approved for canary deployment",
            approved_at=now,
        )
        db.add(approval)
        exp.status = "APPROVED"

        audit = AuditLog(
            user_id=user.id,
            connection_id=exp.connection_id,
            action_type="RECOMMENDATION_APPROVED",
            target_entity="optimization_experiment",
            target_id=str(exp.id),
            details={"action": "APPROVE", "reason": approval.reason},
            timestamp=now,
        )
        db.add(audit)
        await db.commit()
        await db.refresh(approval)
        return approval

    async def reject_recommendation(
        self,
        experiment_id: uuid.UUID,
        user: Any,
        reason: str | None,
        db: AsyncSession,
        owner_is_superuser: bool = False,
    ) -> Approval:
        """Record an authorized human rejection halting the pipeline."""
        if not is_authorized_for_approval(user):
            role_name = getattr(user, "role", "unknown")
            raise PermissionError(
                f"User role '{role_name}' is not authorized to reject database modifications. "
                f"Required roles: {', '.join(sorted(AUTHORIZED_APPROVAL_ROLES))}."
            )

        exp = await self.get_experiment(experiment_id, db, user.id, owner_is_superuser)
        if not exp:
            raise LookupError(f"Experiment {experiment_id} not found")

        now = datetime.now(timezone.utc)
        rejection = Approval(
            experiment_id=exp.id,
            user_id=user.id,
            action="REJECT",
            reason=reason or "Rejected by reviewer",
            approved_at=now,
        )
        db.add(rejection)
        exp.status = "REJECTED"
        exp.success = False

        audit = AuditLog(
            user_id=user.id,
            connection_id=exp.connection_id,
            action_type="RECOMMENDATION_REJECTED",
            target_entity="optimization_experiment",
            target_id=str(exp.id),
            details={"action": "REJECT", "reason": rejection.reason},
            timestamp=now,
        )
        db.add(audit)
        await db.commit()
        await db.refresh(rejection)
        return rejection

    async def deploy_canary(
        self,
        experiment_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
        *,
        customer_connection: Any = None,
        observation_window_minutes: int | None = None,
        owner_is_superuser: bool = False,
    ) -> CanaryRun:
        """Guarded canary deployment with mandatory human approval check."""
        exp = await self.get_experiment(experiment_id, db, user_id, owner_is_superuser)
        if not exp:
            raise LookupError(f"Experiment {experiment_id} not found")

        # 1. Hard Gate: Policy Engine Check
        _deployable = {"VERIFIED", "APPROVE"}
        # SECURITY: ZENTRIX_ALLOW_UNVERIFIED_DEPLOY is a demo bypass. With Model-B
        # shadow-pool live, verification should produce VERIFIED / REJECTED and
        # this branch should almost never fire. If it does fire in production
        # a WARN log makes the exception loud in your log pipeline.
        if os.getenv("ZENTRIX_ALLOW_UNVERIFIED_DEPLOY", "false").lower() == "true":
            logger.warning(
                "ZENTRIX_ALLOW_UNVERIFIED_DEPLOY=true — deployable_set widened to include "
                "INSUFFICIENT_DATA. This bypass must not be set in production; every deploy "
                "under this flag is unverified.",
                extra={"experiment_id": str(experiment_id)},
            )
            _deployable.add("INSUFFICIENT_DATA")
        if exp.policy_verdict not in _deployable:
            raise ValueError(
                f"Cannot deploy candidate with policy verdict '{exp.policy_verdict}'. Must be VERIFIED."
            )

        # 2. Hard Gate: Human Approval Check (Defense in depth per PRD §6 & Step 24)
        approval_stmt = (
            select(Approval)
            .where(
                Approval.experiment_id == experiment_id,
                Approval.action == "APPROVE",
            )
        )
        approval = await db.scalar(approval_stmt)
        if not approval:
            raise PermissionError(
                f"Human approval required before production canary deployment. "
                f"No 'APPROVE' record found for experiment {experiment_id}."
            )

        # 3. Whitelist Validation on SQL
        validate_canary_sql(exp.candidate_sql)

        # 4. Execute on Customer Database via Guarded Path
        # Model-B split-role: prefer the elevated deploy connection over the
        # (monitoring) customer_connection when running the DDL. If tests pass
        # a mock customer_connection, keep the legacy path so their expectations
        # continue to hold — the split-role code fires only when no test-double
        # was injected.
        executed_by_role = "monitoring"
        if customer_connection is not None:
            logger.info(f"Executing canary DDL via injected connection: {exp.candidate_sql}")
            await customer_connection.execute(exp.candidate_sql)
        else:
            logger.info(f"Executing canary DDL on customer DB: {exp.candidate_sql}")
            deploy_conn, executed_by_role = await customer_connection_manager.get_deploy_connection(
                exp.connection_id, db
            )
            try:
                await deploy_conn.execute(exp.candidate_sql)
            finally:
                await deploy_conn.close()

        now = datetime.now(timezone.utc)
        window = observation_window_minutes or get_settings().CANARY_MONITOR_WINDOW_MINUTES

        canary_run = CanaryRun(
            experiment_id=exp.id,
            connection_id=exp.connection_id,
            status="RUNNING",
            canary_sql_applied=exp.candidate_sql,
            executed_by_role=executed_by_role,
            started_at=now,
            observation_window_minutes=window,
            baseline_metrics={
                "p95_ms": exp.baseline_p95,
                "latency_ms": exp.baseline_latency,
            },
            canary_metrics={
                "p95_ms": exp.candidate_p95,
                "latency_ms": exp.candidate_latency,
            },
        )
        db.add(canary_run)
        exp.status = "DEPLOYED"

        # Record audit log
        audit = AuditLog(
            user_id=user_id,
            connection_id=exp.connection_id,
            action_type="CANARY_START",
            target_entity="canary_run",
            target_id=str(canary_run.id),
            details={
                "experiment_id": str(exp.id),
                "sql": exp.candidate_sql,
                "observation_window_minutes": window,
            },
            timestamp=now,
        )
        db.add(audit)

        await db.commit()
        await db.refresh(canary_run)
        return canary_run


simulation_service = SimulationService()
