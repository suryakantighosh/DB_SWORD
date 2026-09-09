"""VACUUM specialist: bloat and autovacuum lag."""

from app.agents.graph_diagnosis import _specialist_node

vacuum_node = _specialist_node("VACUUM")

__all__ = ["vacuum_node"]
