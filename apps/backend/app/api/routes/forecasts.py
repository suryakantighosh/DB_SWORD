"""Forecasting & Model Performance API Endpoints.

Reference: PRD.md §5 Feature 3, §12 & ARCHITECTURE.md §1, §4, §10
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_connection_user as get_current_user, get_db_session
from app.models.user import User
from app.schemas.forecast import (
    ForecastResponse,
    ModelPerformanceResponse,
)
from app.services.forecast_service import forecast_service

router = APIRouter(tags=["Forecasting & Model Performance"])


@router.get("/forecast/{connectionId}", response_model=ForecastResponse)
@router.get("/connections/{connectionId}/forecasts", response_model=ForecastResponse)
@router.get("/connections/{connectionId}/forecasts", response_model=ForecastResponse)
async def get_connection_forecast(
    connectionId: uuid.UUID,
    query_id: int | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Get 7-day degradation risk forecast and probability curve for queries on a database connection."""
    try:
        return await forecast_service.generate_forecast(
            connection_id=connectionId,
            query_id=query_id,
            db=db,
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.get("/models/performance", response_model=ModelPerformanceResponse)
@router.get("/forecasts/models/performance", response_model=ModelPerformanceResponse)
async def get_models_performance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """Retrieve model evaluation metrics: MAE over time, calibration score, and Evidently drift reports."""
    try:
        return await forecast_service.get_model_performance(db=db)
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)) from exc


@router.get("/forecasts/{id}/stream")
async def stream_forecast_progress(
    id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> EventSourceResponse:
    """Server-Sent Events (SSE) streaming live forecast horizon computation."""
    async def sse_event_stream() -> AsyncGenerator[dict[str, Any], None]:
        async for event in forecast_service.stream_forecast_execution(connection_id=id, db=db):
            yield {
                "event": event.get("event", "forecast_progress"),
                "data": json.dumps(event.get("data", {})),
            }

    return EventSourceResponse(sse_event_stream())

@router.get("/forecasts", response_model=list)
async def list_forecasts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> Any:
    """List forecasts (empty until per-connection forecasts are materialized)."""
    return []


# ── Arc C2 — Rollout phase state for the frontend badge ─────────────────────
from app.ml.bandit.policy import (
    RolloutPhase,
    current_rollout_phase,
    PHASE_2_MIN_LABELLED_EXPERIMENTS,
    PHASE_3_MIN_LABELLED_EXPERIMENTS,
)
from sqlalchemy import func as _func, select
from app.models.experiment import ModelPrediction as _ModelPrediction


@router.get("/forecasts/rollout-phase")
async def get_rollout_phase(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    """Deterministic rollout phase + progress toward the next graduation.

    Frontend renders this as a small badge on the forecasts page. Never fails —
    on any error returns PHASE_1_RULE_BASED with zeroed counters.
    """
    try:
        current = await current_rollout_phase(db)
        labelled = int(await db.scalar(
            select(_func.count()).select_from(_ModelPrediction).where(
                _ModelPrediction.actual.is_not(None)
            )
        ) or 0)
    except Exception:
        return {
            "current_phase": RolloutPhase.PHASE_1_RULE_BASED.value,
            "labelled_experiments": 0,
            "next_threshold": PHASE_2_MIN_LABELLED_EXPERIMENTS,
            "progress_pct": 0.0,
            "advisory": True,
            "bandit_live": False,
        }

    if current == RolloutPhase.PHASE_1_RULE_BASED:
        next_threshold = PHASE_2_MIN_LABELLED_EXPERIMENTS
    elif current == RolloutPhase.PHASE_2_SUPERVISED:
        next_threshold = PHASE_3_MIN_LABELLED_EXPERIMENTS
    else:
        next_threshold = labelled

    progress = min(100.0, (labelled / next_threshold) * 100.0) if next_threshold > 0 else 100.0
    return {
        "current_phase": current.value,
        "labelled_experiments": labelled,
        "next_threshold": next_threshold,
        "progress_pct": round(progress, 1),
        "bandit_live": current == RolloutPhase.PHASE_4_OFFLINE_EVALUATED,
    }

