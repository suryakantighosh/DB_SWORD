"""IO_BUFFER specialist: buffer pressure and temp-file spills."""

from app.agents.graph_diagnosis import _specialist_node

io_buffer_node = _specialist_node("IO_BUFFER")

__all__ = ["io_buffer_node"]
