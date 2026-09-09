"""CONCURRENCY specialist: lock contention and connection saturation."""

from app.agents.graph_diagnosis import _specialist_node

concurrency_node = _specialist_node("CONCURRENCY")

__all__ = ["concurrency_node"]
