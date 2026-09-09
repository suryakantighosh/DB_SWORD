"""PLANNER specialist: query-plan root causes (plan flips, cardinality, stale stats)."""

from app.agents.graph_diagnosis import _specialist_node

# The specialist nodes are produced by a domain-scoped factory in the graph
# module; binding it here gives the package a stable per-agent handle.
planner_node = _specialist_node("PLANNER")

__all__ = ["planner_node"]
