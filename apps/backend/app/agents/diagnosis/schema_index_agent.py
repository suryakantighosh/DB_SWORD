"""SCHEMA_INDEX specialist: missing/unused indexes and sequential scans."""

from app.agents.graph_diagnosis import _specialist_node

schema_index_node = _specialist_node("SCHEMA_INDEX")

__all__ = ["schema_index_node"]
