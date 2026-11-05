"""AI Team OS — memory management module.

Provides three-temperature memory management (MemoryStore), context recovery (ContextRecovery),
and a pluggable memory backend (the MemoryBackend Protocol).
"""

from aiteam.memory.backends import MemoryBackend
from aiteam.memory.backends.sqlite_backend import SqliteMemoryBackend
from aiteam.memory.recovery import ContextRecovery
from aiteam.memory.store import MemoryStore

__all__ = [
    "ContextRecovery",
    "MemoryBackend",
    "MemoryStore",
    "SqliteMemoryBackend",
]
