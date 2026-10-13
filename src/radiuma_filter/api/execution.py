"""Execution policies, events, errors, and result model."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol, Self, runtime_checkable

from .errors import ResultStateError
from ..filters.core import Filter, FilterComponent
from .identity import ApplicationSessionId, CorrelationId, ExecutionId, JournalPosition
from .logs import ExecutionLogs

class ErrorPolicy(str, Enum):
    RAISE = "raise"
    COLLECT = "collect"

class DevicePreference(str, Enum):
    AUTO = "auto"
    CPU = "cpu"
    GPU = "gpu"

@dataclass(frozen=True, slots=True)
class ExecutionOptions:
    max_in_flight: int = 1
    error_policy: ErrorPolicy = ErrorPolicy.RAISE
    device: DevicePreference = DevicePreference.AUTO
    progress: bool = True

    def __post_init__(self) -> None:
        maximum = int(self.max_in_flight)
        if maximum < 1:
            raise ValueError("max_in_flight must be at least 1")
        object.__setattr__(self, "max_in_flight", maximum)
        if not isinstance(self.error_policy, ErrorPolicy):
            object.__setattr__(self, "error_policy", ErrorPolicy(self.error_policy))
        if not isinstance(self.device, DevicePreference):
            object.__setattr__(self, "device", DevicePreference(self.device))
        object.__setattr__(self, "progress", bool(self.progress))

class EventType(str, Enum):
    EXECUTION_STARTED = "execution_started"
    EXECUTION_COMPLETED = "execution_completed"
    EXECUTION_FAILED = "execution_failed"
    FILTER_STARTED = "filter_started"
    FILTER_COMPLETED = "filter_completed"
    FILTER_FAILED = "filter_failed"
    PROBLEM = "problem"

@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionEvent:
    """Immutable semantic notification, deliberately separate from logs."""
    type: EventType
    execution_id: ExecutionId
    timestamp: datetime
    sequence: int = 0
    correlation_id: CorrelationId | None = None
    root_component: FilterComponent | None = None
    filter: Filter | None = None
    input_index: int | None = None
    error: BaseException | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", self.type if isinstance(self.type, EventType) else EventType(self.type))
        object.__setattr__(self, "execution_id", ExecutionId.parse(self.execution_id))
        if not isinstance(self.timestamp, datetime) or self.timestamp.tzinfo is None:
            raise ValueError("ExecutionEvent.timestamp must be timezone-aware")
        sequence = int(self.sequence)
        if sequence < 0:
            raise ValueError("ExecutionEvent.sequence cannot be negative")
        object.__setattr__(self, "sequence", sequence)
        if self.correlation_id is not None:
            object.__setattr__(self, "correlation_id", CorrelationId.parse(self.correlation_id))
        if self.root_component is not None and not isinstance(self.root_component, FilterComponent):
            raise TypeError("root_component must be FilterComponent")
        if self.filter is not None and not isinstance(self.filter, Filter):
            raise TypeError("filter must be Filter")
        if self.input_index is not None:
            index = int(self.input_index)
            if index < 0:
                raise ValueError("input_index cannot be negative")
            object.__setattr__(self, "input_index", index)

@runtime_checkable
class ExecutionObserver(Protocol):
    def on_event(self, event: ExecutionEvent) -> None: ...

class ErrorStage(str, Enum):
    DISCOVER = "discover"
    LOAD = "load"
    FILTER = "filter"

@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionError:
    stage: ErrorStage
    exception: BaseException
    input_index: int | None = None
    input_key: str | None = None
    root_component: FilterComponent | None = None
    filter: Filter | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ErrorStage):
            object.__setattr__(self, "stage", ErrorStage(self.stage))
        if not isinstance(self.exception, BaseException):
            raise TypeError("exception must be BaseException")
        if self.input_index is not None:
            index = int(self.input_index)
            if index < 0:
                raise ValueError("input_index cannot be negative")
            object.__setattr__(self, "input_index", index)
        if self.input_key is not None:
            object.__setattr__(self, "input_key", str(self.input_key))
        if self.root_component is not None and not isinstance(self.root_component, FilterComponent):
            raise TypeError("root_component must be FilterComponent")
        if self.filter is not None and not isinstance(self.filter, Filter):
            raise TypeError("filter must be Filter")

class _OutputRecord:
    """All independent root outcomes for one logical input.

    This record is intentionally private to the result layer. Lookup is
    identity-based: ``output[filter_definition]``.
    """

    __slots__ = ("index", "key", "errors", "_values", "_roots")

    def __init__(
        self,
        *,
        index: int,
        key: str,
        roots: tuple[FilterComponent, ...],
        values: tuple[tuple[FilterComponent, Any], ...],
        errors: tuple[ExecutionError, ...] = (),
    ) -> None:
        self.index = int(index)
        self.key = str(key)
        self.errors = tuple(errors)
        self._roots = tuple(roots)
        self._values = tuple(values)

    def __getitem__(self, component: FilterComponent) -> Any:
        if not isinstance(component, FilterComponent):
            raise TypeError("component must be FilterComponent")
        if not any(root is component for root in self._roots):
            raise KeyError("component is not a root of this execution")
        for root, value in self._values:
            if root is component:
                return value
        for error in self.errors:
            if error.root_component is component:
                raise LookupError(
                    f"root component failed for logical input {self.index}: "
                    f"{error.exception}"
                ) from error.exception
        raise LookupError(f"no output is available for logical input {self.index}")

    def __repr__(self) -> str:
        return (
            f"OutputRecord(index={self.index}, key={self.key!r}, "
            f"errors={len(self.errors)})"
        )

class ExecutionResult:
    """Lazy single-stream execution handle.

    ``output()`` is the only public output-access operation. It always yields
    lightweight per-input output records, independent of input and root-component cardinality.
    """

    __slots__ = (
        "_id",
        "_roots",
        "_logs",
        "_stream_factory",
        "_stream_close",
        "_errors",
        "_lock",
        "_state",
        "_cache",
        "_active_iterator",
    )

    _READY = "ready"
    _STREAMING = "streaming"
    _CONSUMED = "consumed"
    _MATERIALIZED = "materialized"
    _CLOSED = "closed"

    def __init__(
        self,
        *,
        execution_id: ExecutionId,
        roots: tuple[FilterComponent, ...],
        logs: ExecutionLogs,
        stream_factory: Callable[[], Iterator[_OutputRecord]],
        stream_close: Callable[[], None],
        errors: list[ExecutionError],
    ) -> None:
        self._id = ExecutionId.parse(execution_id)
        self._roots = tuple(roots)
        self._logs = logs
        self._stream_factory = stream_factory
        self._stream_close = stream_close
        self._errors = errors
        self._lock = threading.RLock()
        self._state = self._READY
        self._cache: tuple[_OutputRecord, ...] | None = None
        self._active_iterator: Iterator[_OutputRecord] | None = None

    @property
    def id(self) -> ExecutionId:
        return self._id

    @property
    def roots(self) -> tuple[FilterComponent, ...]:
        return self._roots

    @property
    def logs(self) -> ExecutionLogs:
        return self._logs

    @property
    def errors(self) -> Sequence[ExecutionError]:
        with self._lock:
            return tuple(self._errors)

    @property
    def is_materialized(self) -> bool:
        with self._lock:
            return self._state == self._MATERIALIZED

    @property
    def is_closed(self) -> bool:
        with self._lock:
            return self._state == self._CLOSED

    def output(self) -> Iterator[_OutputRecord]:
        with self._lock:
            if self._state == self._MATERIALIZED:
                return iter(self._cache or ())
            iterator = self._start_stream_locked()

        def consume() -> Iterator[_OutputRecord]:
            try:
                yield from iterator
            finally:
                self._finish_stream(self._CONSUMED)

        return consume()

    def materialize(self) -> Self:
        with self._lock:
            if self._state == self._MATERIALIZED:
                return self
            iterator = self._start_stream_locked()

        try:
            cache = tuple(iterator)
        except BaseException:
            self._finish_stream(self._CONSUMED)
            raise
        self._finish_stream(self._MATERIALIZED, cache=cache)
        return self

    def close(self) -> None:
        with self._lock:
            if self._state == self._CLOSED:
                return
            self._state = self._CLOSED
            iterator = self._active_iterator
            self._active_iterator = None
        try:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
        finally:
            self._stream_close()

    def __enter__(self) -> Self:
        if self.is_closed:
            raise ResultStateError("ExecutionResult is closed")
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool | None:
        self.close()
        return None

    def _start_stream_locked(self) -> Iterator[_OutputRecord]:
        if self._state == self._CLOSED:
            raise ResultStateError("ExecutionResult is closed")
        if self._state != self._READY:
            raise ResultStateError(
                "ExecutionResult.output() is single-pass; call materialize() "
                "before streaming when repeatable output is required"
            )
        self._state = self._STREAMING
        iterator = self._stream_factory()
        self._active_iterator = iterator
        return iterator

    def _finish_stream(
        self,
        state: str,
        *,
        cache: tuple[_OutputRecord, ...] | None = None,
    ) -> None:
        try:
            self._stream_close()
        finally:
            with self._lock:
                self._active_iterator = None
                if self._state != self._CLOSED:
                    self._cache = cache
                    self._state = state

__all__ = [
    "ApplicationSessionId", "CorrelationId", "ExecutionId", "JournalPosition",
    "DevicePreference", "ErrorPolicy", "ErrorStage", "EventType",
    "ExecutionError", "ExecutionEvent", "ExecutionObserver", "ExecutionOptions", "ExecutionResult",
]
