"""Typed opaque identifiers shared by execution and diagnostics."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Self

@dataclass(frozen=True, slots=True, order=True)
class _OpaqueId:
    value: str

    def __post_init__(self) -> None:
        value = str(self.value or "").strip()
        if not value:
            raise ValueError(f"{type(self).__name__} is required")
        object.__setattr__(self, "value", value)

    @classmethod
    def new(cls) -> Self:
        return cls(uuid.uuid4().hex)

    @classmethod
    def parse(cls, value: object) -> Self:
        if isinstance(value, cls):
            return value
        return cls(str(value))

    def __str__(self) -> str:
        return self.value

class ApplicationSessionId(_OpaqueId):
    """Identity of one private filtering runtime session."""

class ExecutionId(_OpaqueId):
    """Identity of one filtering execution attempt."""

class CorrelationId(_OpaqueId):
    """Causal correlation token used by events and diagnostics."""

@dataclass(frozen=True, slots=True, order=True)
class JournalPosition:
    """Monotonic position in the canonical diagnostic journal."""
    value: int

    def __post_init__(self) -> None:
        value = int(self.value)
        if value < 0:
            raise ValueError("JournalPosition must be non-negative")
        object.__setattr__(self, "value", value)

    def __int__(self) -> int:
        return self.value

    def __str__(self) -> str:
        return str(self.value)

__all__ = [
    "ApplicationSessionId",
    "CorrelationId",
    "ExecutionId",
    "JournalPosition",
]
